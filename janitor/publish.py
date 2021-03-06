#!/usr/bin/python3
# Copyright (C) 2019 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

"""Publishing VCS changes."""

from aiohttp.web_middlewares import normalize_path_middleware
from aiohttp import web
from datetime import datetime, timedelta
import asyncio
import functools
import gpg
from io import BytesIO
import json
import os
import shlex
import sys
import time
from typing import Dict, List, Optional, Any, Tuple
import uuid

from breezy import urlutils
from breezy.bzr.smart import medium
from breezy.transport import get_transport_from_url


from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    push_to_gateway,
    REGISTRY,
)

from silver_platter.proposal import (
    iter_all_mps,
    Hoster,
    hosters,
    )
from silver_platter.utils import (
    open_branch,
    BranchMissing,
    BranchUnavailable,
    full_branch_url,
    )

from breezy.errors import PermissionDenied
from breezy.propose import (
    get_proposal_by_url,
    HosterLoginRequired,
    UnsupportedHoster,
    )
from breezy.transport import Transport
import breezy.plugins.gitlab  # noqa: F401
import breezy.plugins.launchpad  # noqa: F401
import breezy.plugins.github  # noqa: F401

from . import (
    state,
    )
from .debian import state as debian_state
from .config import read_config
from .prometheus import setup_metrics
from .pubsub import Topic, pubsub_handler, pubsub_reader
from .schedule import (
    full_command,
    do_schedule,
    TRANSIENT_ERROR_RESULT_CODES,
    )
from .trace import note, warning
from .vcs import (
    VcsManager,
    LocalVcsManager,
    get_run_diff,
    bzr_to_browse_url,
    )

EXISTING_RUN_RETRY_INTERVAL = 30

MODE_SKIP = 'skip'
MODE_BUILD_ONLY = 'build-only'
MODE_PUSH = 'push'
MODE_PUSH_DERIVED = 'push-derived'
MODE_PROPOSE = 'propose'
MODE_ATTEMPT_PUSH = 'attempt-push'
MODE_BTS = 'bts'
SUPPORTED_MODES = [
    MODE_PUSH,
    MODE_SKIP,
    MODE_BUILD_ONLY,
    MODE_PUSH_DERIVED,
    MODE_PROPOSE,
    MODE_ATTEMPT_PUSH,
    MODE_BTS,
    ]


proposal_rate_limited_count = Counter(
    'proposal_rate_limited',
    'Number of attempts to create a proposal that was rate-limited',
    ['package', 'suite'])
open_proposal_count = Gauge(
    'open_proposal_count', 'Number of open proposals.',
    labelnames=('maintainer',))
merge_proposal_count = Gauge(
    'merge_proposal_count', 'Number of merge proposals by status.',
    labelnames=('status',))
last_publish_pending_success = Gauge(
    'last_publish_pending_success',
    'Last time pending changes were successfully published')
publish_latency = Histogram(
    'publish_latency', 'Delay between build finish and publish.')


class RateLimited(Exception):
    """A rate limit was reached."""


class RateLimiter(object):

    def set_mps_per_maintainer(
            self, mps_per_maintainer: Dict[str, Dict[str, int]]
            ) -> None:
        raise NotImplementedError(self.set_mps_per_maintainer)

    def check_allowed(self, maintainer_email: str) -> None:
        raise NotImplementedError(self.check_allowed)

    def inc(self, maintainer_email: str) -> None:
        raise NotImplementedError(self.inc)


class MaintainerRateLimiter(RateLimiter):

    _open_mps_per_maintainer: Optional[Dict[str, int]]

    def __init__(self, max_mps_per_maintainer: Optional[int] = None):
        self._max_mps_per_maintainer = max_mps_per_maintainer
        self._open_mps_per_maintainer = None

    def set_mps_per_maintainer(
            self, mps_per_maintainer: Dict[str, Dict[str, int]]):
        self._open_mps_per_maintainer = mps_per_maintainer['open']

    def check_allowed(self, maintainer_email: str):
        if not self._max_mps_per_maintainer:
            return
        if self._open_mps_per_maintainer is None:
            # Be conservative
            raise RateLimited('Open mps per maintainer not yet determined.')
        current = self._open_mps_per_maintainer.get(maintainer_email, 0)
        if current > self._max_mps_per_maintainer:
            raise RateLimited(
                'Maintainer %s already has %d merge proposal open (max: %d)'
                % (maintainer_email, current, self._max_mps_per_maintainer))

    def inc(self, maintainer_email: str):
        if self._open_mps_per_maintainer is None:
            return
        self._open_mps_per_maintainer.setdefault(maintainer_email, 0)
        self._open_mps_per_maintainer[maintainer_email] += 1


class NonRateLimiter(RateLimiter):

    def check_allowed(self, email):
        pass

    def inc(self, maintainer_email):
        pass

    def set_mps_per_maintainer(self, mps_per_maintainer):
        pass


class SlowStartRateLimiter(RateLimiter):

    def __init__(self, max_mps_per_maintainer=None):
        self._max_mps_per_maintainer = max_mps_per_maintainer
        self._open_mps_per_maintainer: Optional[Dict[str, int]] = None
        self._merged_mps_per_maintainer: Optional[Dict[str, int]] = None

    def check_allowed(self, email: str) -> None:
        if (self._open_mps_per_maintainer is None or
                self._merged_mps_per_maintainer is None):
            # Be conservative
            raise RateLimited('Open mps per maintainer not yet determined.')
        current = self._open_mps_per_maintainer.get(email, 0)
        if (self._max_mps_per_maintainer and
                current >= self._max_mps_per_maintainer):
            raise RateLimited(
                'Maintainer %s already has %d merge proposal open (absmax: %d)'
                % (email, current, self._max_mps_per_maintainer))
        limit = self._merged_mps_per_maintainer.get(email, 0) + 1
        if current >= limit:
            raise RateLimited(
                'Maintainer %s has %d merge proposals open (current cap: %d)'
                % (email, current, limit))

    def inc(self, maintainer_email: str):
        if self._open_mps_per_maintainer is None:
            return
        self._open_mps_per_maintainer.setdefault(maintainer_email, 0)
        self._open_mps_per_maintainer[maintainer_email] += 1

    def set_mps_per_maintainer(
            self, mps_per_maintainer: Dict[str, Dict[str, int]]):
        self._open_mps_per_maintainer = mps_per_maintainer['open']
        self._merged_mps_per_maintainer = mps_per_maintainer['merged']


class PublishFailure(Exception):

    def __init__(self, mode: str, code: str, description: str):
        self.mode = mode
        self.code = code
        self.description = description


async def derived_branch_name(conn, run, role):
    # TODO(jelmer): Add package name if there are more packages living in this
    # repository
    if role == 'main':
        name = run.branch_name
    else:
        name = '%s/%s' % (run.branch_name, role)

    has_cotenants = await state.has_cotenants(
        conn, run.package, run.branch_url)

    if has_cotenants:
        return name + '/' + run.package
    else:
        return name


async def publish_one(
        suite: str, pkg: str, command, subworker_result, main_branch_url: str,
        mode: str, role: str, revision: bytes, log_id: str,
        derived_branch_name: str, maintainer_email: str,
        vcs_manager: VcsManager,
        legacy_local_branch_name: str, topic_merge_proposal,
        rate_limiter: RateLimiter, dry_run: bool, differ_url: str,
        external_url: str, require_binary_diff: bool = False,
        possible_hosters=None,
        possible_transports: Optional[List[Transport]] = None,
        allow_create_proposal: Optional[bool] = None,
        reviewers: Optional[List[str]] = None,
        derived_owner: Optional[str] = None,
        result_tags: Optional[List[Tuple[str, bytes]]] = None):
    """Publish a single run in some form.

    Args:
      suite: The suite name
      pkg: Package name
      command: Command that was run
    """
    assert mode in SUPPORTED_MODES, 'mode is %r' % mode
    local_branch = vcs_manager.get_branch(pkg, '%s/%s' % (suite, role))
    if local_branch is None:
        local_branch = vcs_manager.get_branch(pkg, legacy_local_branch_name)
        if local_branch is None:
            raise PublishFailure(
                mode, 'result-branch-not-found',
                'can not find local branch for %s / %s (%s)' % (
                    pkg, legacy_local_branch_name, log_id))

    request = {
        'dry-run': dry_run,
        'suite': suite,
        'package': pkg,
        'command': command,
        'subworker_result': subworker_result,
        'main_branch_url': main_branch_url.rstrip('/'),
        'local_branch_url': full_branch_url(local_branch),
        'derived_branch_name': derived_branch_name,
        'mode': mode,
        'role': role,
        'log_id': log_id,
        'require-binary-diff': require_binary_diff,
        'allow_create_proposal': allow_create_proposal,
        'external_url': external_url,
        'differ_url': differ_url,
        'derived-owner': derived_owner,
        'revision': revision.decode('utf-8'),
        'reviewers': reviewers}

    if result_tags:
        request['tags'] = {
            n: r.decode('utf-8') for (n, r) in result_tags}
    else:
        request['tags'] = {}

    args = [sys.executable, '-m', 'janitor.publish_one']

    p = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE)

    (stdout, stderr) = await p.communicate(json.dumps(request).encode())

    if p.returncode == 1:
        try:
            response = json.loads(stdout.decode())
        except json.JSONDecodeError:
            raise PublishFailure(
                mode, 'publisher-invalid-response', stderr.decode())
        sys.stderr.write(stderr.decode())
        raise PublishFailure(mode, response['code'], response['description'])

    if p.returncode == 0:
        response = json.loads(stdout.decode())

        proposal_url = response.get('proposal_url')
        branch_name = response.get('branch_name')
        is_new = response.get('is_new')

        if proposal_url and is_new:
            topic_merge_proposal.publish(
                {'url': proposal_url, 'status': 'open', 'package': pkg})

            merge_proposal_count.labels(status='open').inc()
            rate_limiter.inc(maintainer_email)
            open_proposal_count.labels(maintainer=maintainer_email).inc()

        return proposal_url, branch_name, is_new

    raise PublishFailure(mode, 'publisher-invalid-response', stderr.decode())


async def publish_pending_new(db, rate_limiter, vcs_manager,
                              topic_publish, topic_merge_proposal,
                              dry_run: bool, external_url: str,
                              differ_url: str,
                              reviewed_only: bool = False,
                              push_limit: Optional[int] = None,
                              require_binary_diff: bool = False):
    start = time.time()
    possible_hosters: List[Hoster] = []
    possible_transports: List[Transport] = []
    actions: Dict[str, int] = {}

    if reviewed_only:
        review_status = ['approved']
    else:
        review_status = ['approved', 'unreviewed']

    async with db.acquire() as conn1, db.acquire() as conn:
        async for (run, value, maintainer_email, uploader_emails,
                   publish_policy, update_changelog,
                   command, unpublished_branches) in state.iter_publish_ready(
                       conn1, review_status=review_status,
                       publishable_only=True):
            if run.revision is None:
                warning(
                    'Run %s is publish ready, but does not have revision set.',
                    run.id)
                continue
            # TODO(jelmer): next try in SQL query
            attempt_count = await state.get_publish_attempt_count(
                conn, run.revision, {'differ-unreachable'})
            try:
                next_try_time = run.times[1] + (
                    2 ** attempt_count * timedelta(hours=1))
            except OverflowError:
                continue
            if datetime.now() < next_try_time:
                note('Not attempting to push %s / %s (%s) due to '
                     'exponential backoff. Next try in %s.',
                     run.package, run.suite, run.id,
                     next_try_time - datetime.now())
                continue
            if push_limit is not None and (
                    MODE_PUSH in publish_policy.values() or
                    MODE_ATTEMPT_PUSH in publish_policy.values()):
                if push_limit == 0:
                    note('Not pushing %s / %s: push limit reached',
                         run.package, run.suite)
                    continue
            actual_modes = {}
            for role, remote_name, base_revision, revision, publish_mode in (
                    unpublished_branches):
                if publish_mode is None:
                    warning('%s: No publish mode for branch with role %s',
                            run.id, role)
                    continue
                actual_modes[role] = await publish_from_policy(
                        conn, rate_limiter, vcs_manager, run,
                        role, maintainer_email, uploader_emails,
                        run.branch_url, topic_publish, topic_merge_proposal,
                        publish_mode, update_changelog, command,
                        possible_hosters=possible_hosters,
                        possible_transports=possible_transports,
                        dry_run=dry_run, external_url=external_url,
                        differ_url=differ_url,
                        require_binary_diff=require_binary_diff,
                        force=False, requestor='publisher (publish pending)')
                actions.setdefault(actual_modes[role], 0)
                actions[actual_modes[role]] += 1
            if MODE_PUSH in actual_modes.values() and push_limit is not None:
                push_limit -= 1

    note('Actions performed: %r', actions)
    note('Done publishing pending changes; duration: %.2fs' % (
         time.time() - start))

    last_publish_pending_success.set_to_current_time()


async def handle_publish_failure(e, conn, run, unchanged_run, bucket):
    from .schedule import (
        do_schedule,
        do_schedule_control,
        )
    code = e.code
    description = e.description
    if e.code == 'merge-conflict':
        note('Merge proposal would cause conflict; restarting.')
        await do_schedule(
            conn, run.package, run.suite,
            requestor='publisher (pre-creation merge conflict)',
            bucket=bucket)
    elif e.code == 'diverged-branches':
        note('Branches have diverged; restarting.')
        await do_schedule(
            conn, run.package, run.suite,
            requestor='publisher (diverged branches)',
            bucket=bucket)
    elif e.code == 'missing-build-diff-self':
        if run.result_code != 'success':
            description = (
                'Missing build diff; run was not actually successful?')
        else:
            description = 'Missing build artifacts, rescheduling'
            await do_schedule(
                conn, run.package, run.suite,
                refresh=True,
                requestor='publisher (missing build artifacts - self)',
                bucket=bucket)
    elif e.code == 'missing-build-diff-control':
        if unchanged_run and unchanged_run.result_code != 'success':
            description = (
                'Missing build diff; last control run failed (%s).' %
                unchanged_run.result_code)
        elif unchanged_run and unchanged_run.has_artifacts():
            description = (
                'Missing build diff due to control run, but successful '
                'control run exists. Rescheduling.')
            await do_schedule_control(
                conn, unchanged_run.package, unchanged_run.revision,
                refresh=True,
                requestor='publisher (missing build artifacts - control)',
                bucket=bucket)
        else:
            description = (
                'Missing binary diff; requesting control run.')
            if run.main_branch_revision is not None:
                await do_schedule_control(
                    conn, run.package, run.main_branch_revision,
                    requestor='publisher (missing control run for diff)',
                    bucket=bucket)
            else:
                warning(
                    'Successful run (%s) does not have main branch '
                    'revision set', run.id)
    return (code, description)


async def publish_from_policy(
        conn, rate_limiter, vcs_manager, run: state.Run,
        role: str, maintainer_email: str,
        uploader_emails: List[str], main_branch_url: str,
        topic_publish, topic_merge_proposal,
        mode: str, update_changelog: str, command: List[str],
        dry_run: bool, external_url: str, differ_url: str,
        possible_hosters: Optional[List[Hoster]] = None,
        possible_transports: Optional[List[Transport]] = None,
        require_binary_diff: bool = False, force: bool = False,
        requestor: Optional[str] = None):
    if not command:
        warning('no command set for %s', run.id)
        return
    expected_command = full_command(update_changelog, command)
    if ' '.join(expected_command) != run.command:
        warning(
            'Not publishing %s/%s: command is different (policy changed?). '
            'Build used %r, now: %r. Rescheduling.',
            run.package, run.suite, run.command, ' '.join(expected_command))
        await do_schedule(
            conn, run.package, run.suite,
            command=expected_command,
            bucket='update-new-mp',
            refresh=True, requestor='publisher (changed policy)')
        return

    publish_id = str(uuid.uuid4())
    if mode in (None, MODE_BUILD_ONLY, MODE_SKIP):
        return
    if run.result_branches is None:
        warning('no result branches for %s', run.id)
        return
    try:
        (remote_branch_name, base_revision,
         revision) = run.get_result_branch(role)
    except KeyError:
        warning('unable to find main branch: %s', run.id)
        return

    main_branch_url = role_branch_url(main_branch_url, remote_branch_name)

    if not force and await state.already_published(
            conn, run.package, run.branch_name, revision, mode):
        return
    if mode in (MODE_PROPOSE, MODE_ATTEMPT_PUSH):
        open_mp = await state.get_open_merge_proposal(
            conn, run.package, run.branch_name)
        if not open_mp:
            try:
                rate_limiter.check_allowed(maintainer_email)
            except RateLimited as e:
                proposal_rate_limited_count.labels(
                    package=run.package, suite=run.suite).inc()
                warning('Not creating proposal for %s/%s: %s',
                        run.package, run.suite, e)
                mode = MODE_BUILD_ONLY
    if mode in (MODE_BUILD_ONLY, MODE_SKIP):
        return

    unchanged_run = await state.get_unchanged_run(
        conn, run.package, base_revision)

    # TODO(jelmer): Make this more generic
    if (unchanged_run and
        unchanged_run.result_code in ('debian-upstream-metadata-invalid', ) and
            run.suite == 'lintian-fixes'):
        require_binary_diff = False

    note('Publishing %s / %r / %s (mode: %s)',
         run.package, run.command, role, mode)
    try:
        proposal_url, branch_name, is_new = await publish_one(
            run.suite, run.package, run.command, run.result,
            main_branch_url, mode, role, revision,
            run.id,
            await derived_branch_name(conn, run, role),
            maintainer_email,
            vcs_manager=vcs_manager,
            legacy_local_branch_name=run.branch_name,
            topic_merge_proposal=topic_merge_proposal,
            dry_run=dry_run, external_url=external_url,
            differ_url=differ_url,
            require_binary_diff=require_binary_diff,
            possible_hosters=possible_hosters,
            possible_transports=possible_transports,
            rate_limiter=rate_limiter,
            result_tags=run.result_tags)
    except PublishFailure as e:
        code, description = await handle_publish_failure(
            e, conn, run, unchanged_run,
            bucket='update-new-mp')
        branch_name = None
        proposal_url = None
        note('Failed(%s): %s', code, description)
    else:
        code = 'success'
        description = 'Success'

    if mode == MODE_ATTEMPT_PUSH:
        if proposal_url:
            mode = MODE_PROPOSE
        else:
            mode = MODE_PUSH

    await state.store_publish(
        conn, run.package, branch_name, base_revision,
        revision, role, mode, code, description,
        proposal_url if proposal_url else None,
        publish_id=publish_id, requestor=requestor)

    if code == 'success' and mode == MODE_PUSH:
        # TODO(jelmer): Call state.update_branch_status() for the
        # main branch URL
        pass

    publish_delay: Optional[timedelta]
    if code == 'success':
        publish_delay = datetime.now() - run.times[1]
        publish_latency.observe(publish_delay.total_seconds())
    else:
        publish_delay = None

    topic_entry: Dict[str, Any] = {
         'id': publish_id,
         'package': run.package,
         'suite': run.suite,
         'proposal_url': proposal_url or None,
         'mode': mode,
         'main_branch_url': main_branch_url,
         'main_branch_browse_url': bzr_to_browse_url(main_branch_url),
         'branch_name': branch_name,
         'result_code': code,
         'result': run.result,
         'run_id': run.id,
         'publish_delay': (
             publish_delay.total_seconds() if publish_delay else None)
         }

    topic_publish.publish(topic_entry)

    if code == 'success':
        return mode


def role_branch_url(url, remote_branch_name):
    if remote_branch_name is None:
        return url
    base_url, params = urlutils.split_segment_parameters(url.rstrip('/'))
    params['branch'] = urlutils.escape(remote_branch_name, safe='')
    return urlutils.join_segment_parameters(base_url, params)


async def publish_and_store(
        db, topic_publish, topic_merge_proposal, publish_id, run, mode,
        role: str, maintainer_email, uploader_emails, vcs_manager,
        rate_limiter,
        dry_run, external_url: str, differ_url: str,
        allow_create_proposal: bool = True,
        require_binary_diff: bool = False, requestor: Optional[str] = None):
    remote_branch_name, base_revision, revision = run.get_result_branch(role)

    main_branch_url = role_branch_url(run.branch_url, remote_branch_name)

    async with db.acquire() as conn:
        try:
            proposal_url, branch_name, is_new = await publish_one(
                run.suite, run.package, run.command, run.result,
                main_branch_url, mode, role, revision,
                run.id,
                await derived_branch_name(conn, run, role),
                maintainer_email, vcs_manager,
                legacy_local_branch_name=run.branch_name, dry_run=dry_run,
                external_url=external_url,
                differ_url=differ_url,
                require_binary_diff=require_binary_diff,
                possible_hosters=None, possible_transports=None,
                allow_create_proposal=allow_create_proposal,
                topic_merge_proposal=topic_merge_proposal,
                rate_limiter=rate_limiter,
                result_tags=run.result_tags)
        except PublishFailure as e:
            await state.store_publish(
                conn, run.package, run.branch_name,
                run.main_branch_revision,
                run.revision, role, e.mode, e.code, e.description,
                None, publish_id=publish_id, requestor=requestor)
            topic_publish.publish({
                'id': publish_id,
                'mode': e.mode,
                'result_code': e.code,
                'description': e.description,
                'package': run.package,
                'suite': run.suite,
                'main_branch_url': run.branch_url,
                'main_branch_browse_url': bzr_to_browse_url(run.branch_url),
                'result': run.result,
                })
            return

        if mode == MODE_ATTEMPT_PUSH:
            if proposal_url:
                mode = MODE_PROPOSE
            else:
                mode = MODE_PUSH

        await state.store_publish(
            conn, run.package, branch_name,
            run.main_branch_revision,
            run.revision, role, mode, 'success', 'Success',
            proposal_url if proposal_url else None,
            publish_id=publish_id, requestor=requestor)

        publish_delay = run.times[1] - datetime.now()
        publish_latency.observe(publish_delay.total_seconds())

        topic_publish.publish(
            {'id': publish_id,
             'package': run.package,
             'suite': run.suite,
             'proposal_url': proposal_url or None,
             'mode': mode,
             'main_branch_url': run.branch_url,
             'main_branch_browse_url': bzr_to_browse_url(run.branch_url),
             'branch_name': branch_name,
             'result_code': 'success',
             'result': run.result,
             'role': role,
             'publish_delay': publish_delay.total_seconds(),
             'run_id': run.id})


async def publish_request(request):
    dry_run = request.app.dry_run
    vcs_manager = request.app.vcs_manager
    rate_limiter = request.app.rate_limiter
    package = request.match_info['package']
    suite = request.match_info['suite']
    role = request.query.get('role')
    post = await request.post()
    mode = post.get('mode')
    async with request.app.db.acquire() as conn:
        try:
            package = await debian_state.get_package(conn, package)
        except IndexError:
            return web.json_response({}, status=400)

        run = await get_last_effective_run(conn, package.name, suite)
        if run is None:
            return web.json_response({}, status=400)

        publish_policy = (await state.get_publish_policy(
            conn, package.name, suite))[0]

        note('Handling request to publish %s/%s', package.name, suite)

    if role is not None:
        roles = [role]
    else:
        roles = [e[0] for e in run.result_branches]

    if mode:
        branches = [(r, mode) for r in roles]
    else:
        branches = [(r, publish_policy.get(r, MODE_SKIP)) for r in roles]

    publish_ids = {}
    loop = asyncio.get_event_loop()
    for role, mode in branches:
        publish_id = str(uuid.uuid4())
        publish_ids[role] = publish_id

        note('.. publishing for role %s: %s', role, mode)

        if mode in (MODE_SKIP, MODE_BUILD_ONLY):
            continue

        loop.create_task(publish_and_store(
            request.app.db, request.app.topic_publish,
            request.app.topic_merge_proposal, publish_id, run, mode,
            role, package.maintainer_email, package.uploader_emails,
            vcs_manager=vcs_manager, rate_limiter=rate_limiter,
            dry_run=dry_run, external_url=request.app.external_url,
            differ_url=request.app.differ_url, allow_create_proposal=True,
            require_binary_diff=False, requestor=post.get('requestor')))

    if not publish_ids:
        return web.json_response(
            {'run_id': run.id, 'code': 'done', 'description': 'Nothing to do'})

    return web.json_response(
        {'run_id': run.id, 'mode': mode, 'publish_ids': publish_ids},
        status=202)


async def credentials_request(request):
    ssh_keys = []
    for entry in os.scandir(os.path.expanduser('~/.ssh')):
        if entry.name.endswith('.pub'):
            with open(entry.path, 'r') as f:
                ssh_keys.extend([line.strip() for line in f.readlines()])
    pgp_keys = []
    for entry in list(request.app.gpg.keylist(secret=True)):
        pgp_keys.append(request.app.gpg.key_export_minimal(entry.fpr).decode())
    hosting = []
    for name, hoster_cls in hosters.items():
        for instance in hoster_cls.iter_instances():
            try:
                current_user = instance.get_current_user()
            except HosterLoginRequired:
                continue
            if current_user:
                current_user_url = instance.get_user_url(current_user)
            else:
                current_user_url = None
            hoster = {
                'kind': name,
                'name': instance.name,
                'url': instance.base_url,
                'user': current_user,
                'user_url': current_user_url,
            }
            hosting.append(hoster)

    return web.json_response({
        'ssh_keys': ssh_keys,
        'pgp_keys': pgp_keys,
        'hosting': hosting,
    })


async def run_web_server(listen_addr: str, port: int,
                         rate_limiter: RateLimiter,
                         vcs_manager: VcsManager, db: state.Database,
                         topic_merge_proposal: Topic, topic_publish: Topic,
                         dry_run: bool, external_url: str,
                         differ_url: str, require_binary_diff: bool = False,
                         push_limit: Optional[int] = None,
                         modify_mp_limit: Optional[int] = None):
    trailing_slash_redirect = normalize_path_middleware(append_slash=True)
    app = web.Application(middlewares=[trailing_slash_redirect])
    app.gpg = gpg.Context(armor=True)
    app.vcs_manager = vcs_manager
    app.db = db
    app.external_url = external_url
    app.differ_url = differ_url
    app.rate_limiter = rate_limiter
    app.modify_mp_limit = modify_mp_limit
    app.topic_publish = topic_publish
    app.topic_merge_proposal = topic_merge_proposal
    app.dry_run = dry_run
    app.push_limit = push_limit
    app.require_binary_diff = require_binary_diff
    setup_metrics(app)
    app.router.add_post("/{suite}/{package}/publish", publish_request)
    app.router.add_get(
        '/ws/publish', functools.partial(pubsub_handler, topic_publish))
    app.router.add_get(
        '/ws/merge-proposal', functools.partial(
            pubsub_handler, topic_merge_proposal))
    app.router.add_post('/check-proposal', check_mp_request)
    app.router.add_post('/scan', scan_request)
    app.router.add_post('/refresh-status', refresh_proposal_status_request)
    app.router.add_post('/autopublish', autopublish_request)
    app.router.add_get('/credentials', credentials_request)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    note('Listening on %s:%s', listen_addr, port)
    await site.start()


async def check_mp_request(request):
    post = await request.post()
    url = post['url']
    try:
        mp = get_proposal_by_url(url)
    except UnsupportedHoster:
        raise web.HTTPNotFound()
    if mp.is_merged():
        status = 'merged'
    elif mp.is_closed():
        status = 'closed'
    else:
        status = 'open'
    async with request.app.db.acquire() as conn:
        try:
            modified = await check_existing_mp(
                conn, mp, status,
                topic_merge_proposal=request.app.topic_merge_proposal,
                vcs_manager=request.app.vcs_manager,
                dry_run=('dry_run' in post),
                external_url=request.app.external_url,
                differ_url=request.app.differ_url,
                rate_limiter=request.app.rate_limiter)
        except NoRunForMergeProposal as e:
            return web.Response(
                status=500,
                text="Unable to find local metadata for %s (%r), skipping." % (
                    e.mp.url, e.revision))
    if modified:
        return web.Response(status=200, text="Merge proposal updated.")
    else:
        return web.Response(status=200, text="Merge proposal not updated.")


async def scan_request(request):
    async def scan():
        async with request.app.db.acquire() as conn:
            await check_existing(
                conn, request.app.rate_limiter,
                request.app.vcs_manager, request.app.topic_merge_proposal,
                dry_run=request.app.dry_run,
                differ_url=request.app.differ_url,
                external_url=request.app.external_url,
                modify_limit=request.app.modify_mp_limit)
    loop = asyncio.get_event_loop()
    loop.create_task(scan())
    return web.Response(status=202, text="Scan started.")


async def refresh_proposal_status_request(request):
    post = await request.post()
    try:
        url = post['url']
    except KeyError:
        raise web.HTTPBadRequest(body="missing url parameter")
    note('Request to refresh proposal status for %s', url)

    async def scan():
        mp = get_proposal_by_url(url)
        async with request.app.db.acquire() as conn:
            if mp.is_merged():
                status = 'merged'
            elif mp.is_closed():
                status = 'closed'
            else:
                status = 'open'
            try:
                await check_existing_mp(
                    conn, mp, status,
                    vcs_manager=request.app.vcs_manager,
                    rate_limiter=request.app.rate_limiter,
                    topic_merge_proposal=request.app.topic_merge_proposal,
                    dry_run=request.app.dry_run,
                    differ_url=request.app.differ_url,
                    external_url=request.app.external_url)
            except NoRunForMergeProposal as e:
                warning('Unable to find local metadata for %s, skipping.',
                        e.mp.url)
    loop = asyncio.get_event_loop()
    loop.create_task(scan())
    return web.Response(status=202, text="Refresh of proposal started.")


async def autopublish_request(request):
    reviewed_only = ('unreviewed' not in request.query)

    async def autopublish():
        await publish_pending_new(
            request.app.db, request.app.rate_limiter, request.app.vcs_manager,
            dry_run=request.app.dry_run,
            topic_publish=request.app.topic_publish,
            external_url=request.app.external_url,
            differ_url=request.app.differ_url,
            topic_merge_proposal=request.app.topic_merge_proposal,
            reviewed_only=reviewed_only, push_limit=request.app.push_limit,
            require_binary_diff=request.app.require_binary_diff)

    loop = asyncio.get_event_loop()
    loop.create_task(autopublish())
    return web.Response(status=202, text="Autopublish started.")


async def process_queue_loop(
        db, rate_limiter, dry_run, vcs_manager, interval,
        topic_merge_proposal, topic_publish,
        external_url: str, differ_url: str,
        auto_publish: bool = True,
        reviewed_only: bool = False, push_limit: Optional[int] = None,
        modify_mp_limit: Optional[int] = None,
        require_binary_diff: bool = False):
    while True:
        cycle_start = datetime.now()
        async with db.acquire() as conn:
            await check_existing(
                conn, rate_limiter, vcs_manager, topic_merge_proposal,
                dry_run=dry_run, external_url=external_url,
                differ_url=differ_url,
                modify_limit=modify_mp_limit)
        if auto_publish:
            await publish_pending_new(
                db, rate_limiter, vcs_manager, dry_run=dry_run,
                external_url=external_url,
                differ_url=differ_url,
                topic_publish=topic_publish,
                topic_merge_proposal=topic_merge_proposal,
                reviewed_only=reviewed_only, push_limit=push_limit,
                require_binary_diff=require_binary_diff)
        cycle_duration = datetime.now() - cycle_start
        to_wait = max(0, interval - cycle_duration.total_seconds())
        note('Waiting %d seconds for next cycle.' % to_wait)
        if to_wait > 0:
            await asyncio.sleep(to_wait)


def is_conflicted(mp):
    try:
        return not mp.can_be_merged()
    except NotImplementedError:
        # TODO(jelmer): Download and attempt to merge locally?
        return None


class NoRunForMergeProposal(Exception):
    """No run matching merge proposal."""

    def __init__(self, mp, revision):
        self.mp = mp
        self.revision = revision


async def get_last_effective_run(conn, package, suite):
    last_success = False
    async for run in state._iter_runs(conn, package=package, suite=suite):
        if run.result_code in ('success', 'nothing-to-do'):
            return run
        elif run.result_code == 'nothing-new-to-do':
            last_success = True
            continue
        elif not last_success:
            return run
    else:
        return None


async def check_existing_mp(
        conn, mp, status, topic_merge_proposal, vcs_manager,
        rate_limiter, dry_run: bool, external_url: str,
        differ_url: str,
        mps_per_maintainer=None,
        possible_transports: Optional[List[Transport]] = None,
        check_only: bool = False) -> bool:
    async def update_proposal_status(mp, status, revision, package_name):
        if status == 'closed':
            # TODO(jelmer): Check if changes were applied manually and mark
            # as applied rather than closed?
            pass
        if status == 'merged':
            merged_by = mp.get_merged_by()
            merged_at = mp.get_merged_at()
            if merged_at is not None:
                merged_at = merged_at.replace(tzinfo=None)
        else:
            merged_by = None
            merged_at = None
        if not dry_run:
            await state.set_proposal_info(
                conn, mp.url, status, revision, package_name, merged_by,
                merged_at)
            topic_merge_proposal.publish(
               {'url': mp.url, 'status': status, 'package': package_name,
                'merged_by': merged_by, 'merged_at': str(merged_at)})

    old_status: Optional[str]
    maintainer_email: Optional[str]
    package_name: Optional[str]
    try:
        (old_revision, old_status, package_name,
            maintainer_email) = await state.get_proposal_info(conn, mp.url)
    except KeyError:
        old_revision = None
        old_status = None
        maintainer_email = None
        package_name = None
    revision = mp.get_source_revision()
    source_branch_url = mp.get_source_branch_url()
    if revision is None:
        if source_branch_url is None:
            warning('No source branch for %r', mp)
            revision = None
            source_branch_name = None
        else:
            try:
                source_branch = open_branch(
                    source_branch_url,
                    possible_transports=possible_transports)
            except (BranchMissing, BranchUnavailable):
                revision = None
                source_branch_name = None
            else:
                revision = source_branch.last_revision()
                source_branch_name = source_branch.name
    else:
        source_branch_name = None
    if source_branch_name is None and source_branch_url is not None:
        source_branch_name = urlutils.split_segment_parameters(
            source_branch_url)[1].get('branch')
    if revision is None:
        revision = old_revision
    if maintainer_email is None:
        target_branch_url = mp.get_target_branch_url()
        package = await debian_state.get_package_by_branch_url(
            conn, target_branch_url)
        if package is not None:
            maintainer_email = package.maintainer_email
            package_name = package.name
        else:
            if revision is not None:
                package_name, maintainer_email = (
                        await debian_state.guess_package_from_revision(
                            conn, revision))
            if package_name is None:
                warning('No package known for %s (%s)',
                        mp.url, target_branch_url)
            else:
                note('Guessed package name (%s) for %s based on revision.',
                     package_name, mp.url)
    if old_status in ('abandoned', 'applied') and status == 'closed':
        status = old_status
    if old_status != status or revision != old_revision:
        await update_proposal_status(mp, status, revision, package_name)
    if maintainer_email is not None and mps_per_maintainer is not None:
        mps_per_maintainer[status].setdefault(maintainer_email, 0)
        mps_per_maintainer[status][maintainer_email] += 1
    if status != 'open':
        return False
    if check_only:
        return False
    try:
        (mp_run,
         (mp_role, mp_remote_branch_name, mp_base_revision,
          mp_revision)) = await state.get_merge_proposal_run(conn, mp.url)
    except KeyError:
        raise NoRunForMergeProposal(mp, revision)

    if mp_remote_branch_name is None:
        target_branch_url = mp.get_target_branch_url()
        if target_branch_url is None:
            warning('No target branch for %r', mp)
        else:
            try:
                mp_remote_branch_name = open_branch(
                    target_branch_url,
                    possible_transports=possible_transports).name
            except (BranchMissing, BranchUnavailable):
                pass

    last_run = await get_last_effective_run(
        conn, mp_run.package, mp_run.suite)
    if last_run is None:
        warning('%s: Unable to find any relevant runs.', mp.url)
        return False

    package = await debian_state.get_package(conn, mp_run.package)
    if package is None:
        warning('%s: Unable to find package.', mp.url)
        return False

    if package.removed:
        note('%s: package has been removed from the archive, '
             'closing proposal.', mp.url)
        if not dry_run:
            try:
                mp.post_comment("""
This merge proposal will be closed, since the package has been removed from the
archive.
""")
            except PermissionDenied as e:
                warning('Permission denied posting comment to %s: %s',
                        mp.url, e)
            try:
                mp.close()
            except PermissionDenied as e:
                warning('Permission denied closing merge request %s: %s',
                        mp.url, e)
                return False
            return True

    if last_run.result_code == 'nothing-to-do':
        # A new run happened since the last, but there was nothing to
        # do.
        note('%s: Last run did not produce any changes, '
             'closing proposal.', mp.url)
        if not dry_run:
            await update_proposal_status(mp, 'applied', revision, package_name)
            try:
                mp.post_comment("""
This merge proposal will be closed, since all remaining changes have been
applied independently.
""")
            except PermissionDenied as e:
                warning('Permission denied posting comment to %s: %s',
                        mp.url, e)
            try:
                mp.close()
            except PermissionDenied as e:
                warning('Permission denied closing merge request %s: %s',
                        mp.url, e)
                return False
        return True

    if last_run.result_code != 'success':
        last_run_age = datetime.now() - last_run.times[1]
        if last_run.result_code in TRANSIENT_ERROR_RESULT_CODES:
            note('%s: Last run failed with transient error (%s). '
                 'Rescheduling.', mp.url, last_run.result_code)
            await do_schedule(
                conn, last_run.package, last_run.suite,
                command=shlex.split(last_run.command),
                bucket='update-existing-mp',
                refresh=False,
                requestor='publisher (transient error)')
        elif last_run_age.days > EXISTING_RUN_RETRY_INTERVAL:
            note('%s: Last run failed (%s) a long time ago (%d days). '
                 'Rescheduling.', mp.url, last_run.result_code,
                 last_run_age.days)
            await do_schedule(
                conn, last_run.package, last_run.suite,
                command=shlex.split(last_run.command),
                bucket='update-existing-mp',
                refresh=False,
                requestor='publisher (retrying failed run after %d days)' %
                last_run_age.days)
        else:
            note('%s: Last run failed (%s). Not touching merge proposal.',
                 mp.url, last_run.result_code)
        return False

    if last_run.branch_name is None:
        note('%s: Last run (%s) does not have branch name set.', mp.url,
             last_run.id)
        return False

    if maintainer_email is None:
        note('%s: No maintainer email known.', mp.url)
        return False

    try:
        (last_run_remote_branch_name, last_run_base_revision,
         last_run_revision) = last_run.get_result_branch(mp_role)
    except KeyError:
        warning('%s: Merge proposal run %s had role %s'
                ' but it is gone now (%s)',
                mp.url, mp_run.id, mp_role, last_run.id)
        return False

    if (last_run_remote_branch_name != mp_remote_branch_name and
            last_run_remote_branch_name is not None):
        warning('%s: Remote branch name has changed: %s => %s, '
                'skipping...', mp.url, mp_remote_branch_name,
                last_run_remote_branch_name,)
        # Note that we require that mp_remote_branch_name is set.
        # For some old runs it is not set because we didn't track
        # the default branch name.
        if not dry_run and mp_remote_branch_name is not None:
            note('%s: Closing merge proposal, since branch for role '
                 '\'%s\' has changed from %s to %s.',
                 mp.url, mp_role, last_run_remote_branch_name,
                 last_run_remote_branch_name)
            await update_proposal_status(
                mp, 'abandoned', revision, package_name)
            try:
                mp.post_comment("""
This merge proposal will be closed, since the branch for the role '%s'
has changed to %s.
""" % (mp_role, last_run_remote_branch_name))
            except PermissionDenied as e:
                warning('Permission denied posting comment to %s: %s',
                        mp.url, e)
            try:
                mp.close()
            except PermissionDenied as e:
                warning('Permission denied closing merge request %s: %s',
                        mp.url, e)
                return False
        return False

    if mp_run.branch_url != last_run.branch_url:
        warning('%s: Remote branch URL appears to have have changed: '
                '%s => %s, skipping.', mp.url, mp_run.branch_url,
                last_run.branch_url)
        return False

        # TODO(jelmer): Don't do this if there's a redirect in place,
        # or if one of the branches has a branch name included and the other
        # doesn't
        if not dry_run:
            await update_proposal_status(
                mp, 'abandoned', revision, package_name)
            try:
                mp.post_comment("""
This merge proposal will be closed, since the branch has moved to %s.
""" % (bzr_to_browse_url(last_run.branch_url), ))
            except PermissionDenied as e:
                warning('Permission denied posting comment to %s: %s',
                        mp.url, e)
            try:
                mp.close()
            except PermissionDenied as e:
                warning('Permission denied closing merge request %s: %s',
                        mp.url, e)
                return False
        return False

    if last_run != mp_run:
        publish_id = str(uuid.uuid4())
        note('%s (%s) needs to be updated (%s => %s).',
             mp.url, mp_run.package, mp_run.id, last_run.id)
        if last_run_revision == mp_revision:
            warning(
                '%s (%s): old run (%s/%s) has same revision as new run (%s/%s)'
                ': %r', mp.url, mp_run.package, mp_run.id, mp_role,
                last_run.id, mp_role, mp_revision)
        if source_branch_name is None:
            source_branch_name = await derived_branch_name(
                conn, last_run, mp_role)
        try:
            mp_url, branch_name, is_new = await publish_one(
                last_run.suite, last_run.package, last_run.command,
                last_run.result,
                role_branch_url(mp_run.branch_url, mp_remote_branch_name),
                MODE_PROPOSE, mp_role, last_run_revision, last_run.id,
                source_branch_name,
                maintainer_email, vcs_manager=vcs_manager,
                legacy_local_branch_name=last_run.branch_name,
                dry_run=dry_run, external_url=external_url,
                differ_url=differ_url, require_binary_diff=False,
                allow_create_proposal=True,
                topic_merge_proposal=topic_merge_proposal,
                rate_limiter=rate_limiter,
                result_tags=last_run.result_tags)
        except PublishFailure as e:
            unchanged_run = await state.get_unchanged_run(
                conn, last_run.package, last_run.main_branch_revision)
            code, description = await handle_publish_failure(
                e, conn, last_run, unchanged_run,
                bucket='update-existing-mp')
            if code == 'empty-merge-proposal':
                # The changes from the merge proposal have already made it in
                # somehow.
                note('%s: Empty merge proposal, changes must have been merged '
                     'some other way. Closing.', mp.url)
                if not dry_run:
                    await update_proposal_status(
                        mp, 'applied', revision, package_name)
                    try:
                        mp.post_comment("""
This merge proposal will be closed, since all remaining changes have been
applied independently.
""")
                    except PermissionDenied as e:
                        warning('Permission denied posting comment to %s: %s',
                                mp.url, e)
                    try:
                        mp.close()
                    except PermissionDenied as e:
                        warning(
                            'Permission denied closing merge request %s: %s',
                            mp.url, e)
                        code = 'empty-failed-to-close'
                        description = (
                            'Permission denied closing merge request: %s' % e)
                code = 'success'
                description = (
                    'Closing merge request for which changes were '
                    'applied independently')
            if code != 'success':
                note('%s: Updating merge proposal failed: %s (%s)',
                     mp.url, code, description)
            if not dry_run:
                await state.store_publish(
                    conn, last_run.package, mp_run.branch_name,
                    last_run_base_revision,
                    last_run_revision, mp_role, e.mode, code,
                    description, mp.url,
                    publish_id=publish_id,
                    requestor='publisher (regular refresh)')
        else:
            if not dry_run:
                await state.store_publish(
                    conn, last_run.package, branch_name,
                    last_run_base_revision,
                    last_run_revision, mp_role, MODE_PROPOSE, 'success',
                    'Succesfully updated', mp_url,
                    publish_id=publish_id,
                    requestor='publisher (regular refresh)')

            if is_new:
                # This can happen when the default branch changes
                warning(
                    "Intended to update proposal %r, but created %r", mp.url,
                    mp_url)
        return True
    else:
        # It may take a while for the 'conflicted' bit on the proposal to
        # be refreshed, so only check it if we haven't made any other
        # changes.
        if is_conflicted(mp):
            note('%s is conflicted. Rescheduling.', mp.url)
            if not dry_run:
                await do_schedule(
                    conn, mp_run.package, mp_run.suite,
                    command=shlex.split(mp_run.command),
                    bucket='update-existing-mp',
                    refresh=True, requestor='publisher (merge conflict)')
        return False


async def check_existing(
        conn, rate_limiter, vcs_manager, topic_merge_proposal,
        dry_run: bool, external_url: str,
        differ_url: str, modify_limit=None):
    mps_per_maintainer: Dict[str, Dict[str, int]] = {
        'open': {}, 'closed': {}, 'merged': {}, 'applied': {},
        'abandoned': {}}
    possible_transports: List[Transport] = []
    status_count = {
        'open': 0, 'closed': 0, 'merged': 0, 'applied': 0,
        'abandoned': 0}

    modified_mps = 0
    check_only = False

    for hoster, mp, status in iter_all_mps():
        status_count[status] += 1
        try:
            modified = await check_existing_mp(
                conn, mp, status, topic_merge_proposal=topic_merge_proposal,
                vcs_manager=vcs_manager, dry_run=dry_run,
                external_url=external_url,
                differ_url=differ_url,
                rate_limiter=rate_limiter,
                possible_transports=possible_transports,
                mps_per_maintainer=mps_per_maintainer,
                check_only=check_only)
        except NoRunForMergeProposal as e:
            warning('Unable to find local metadata for %s, skipping.',
                    e.mp.url)
            modified = False

        if modified:
            modified_mps += 1
            if modify_limit and modified_mps > modify_limit:
                warning('Already modified %d merge proposals, '
                        'waiting with the rest.', modified_mps)
                check_only = True

    for status, count in status_count.items():
        merge_proposal_count.labels(status=status).set(count)

    rate_limiter.set_mps_per_maintainer(mps_per_maintainer)
    for maintainer_email, count in mps_per_maintainer['open'].items():
        open_proposal_count.labels(maintainer=maintainer_email).set(count)


async def listen_to_runner(db, rate_limiter, vcs_manager, runner_url,
                           topic_publish, topic_merge_proposal, dry_run: bool,
                           external_url: str, differ_url: str,
                           require_binary_diff: bool = False):
    async def process_run(conn, run, package):
        publish_policy, update_changelog, command = (
            await state.get_publish_policy(
                conn, run.package, run.suite))
        for role, mode in publish_policy.items():
            await publish_from_policy(
                conn, rate_limiter, vcs_manager,
                run, role, package.maintainer_email, package.uploader_emails,
                package.branch_url,
                topic_publish, topic_merge_proposal, mode,
                update_changelog, command, dry_run=dry_run,
                external_url=external_url, differ_url=differ_url,
                require_binary_diff=require_binary_diff,
                force=True, requestor='runner')
    from aiohttp.client import ClientSession
    import urllib.parse
    url = urllib.parse.urljoin(runner_url, 'ws/result')
    async with ClientSession() as session:
        async for result in pubsub_reader(session, url):
            if result['code'] != 'success':
                continue
            async with db.acquire() as conn:
                # TODO(jelmer): Fold these into a single query ?
                package = await debian_state.get_package(
                    conn, result['package'])
                run = await state.get_run(conn, result['log_id'])
                if run.suite != 'unchanged':
                    await process_run(conn, run, package)
                else:
                    async for run in state.iter_last_runs(
                            conn, main_branch_revision=run.revision):
                        if run.package != package.name:
                            continue
                        if run.suite != 'unchanged':
                            await process_run(conn, run, package)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='janitor.publish')
    parser.add_argument(
        '--max-mps-per-maintainer',
        default=0,
        type=int,
        help='Maximum number of open merge proposals per maintainer.')
    parser.add_argument(
        "--dry-run",
        help="Create branches but don't push or propose anything.",
        action="store_true", default=False)
    parser.add_argument(
        '--prometheus', type=str,
        help='Prometheus push gateway to export to.')
    parser.add_argument(
        '--once', action='store_true',
        help="Just do one pass over the queue, don't run as a daemon.")
    parser.add_argument(
        '--listen-address', type=str,
        help='Listen address', default='localhost')
    parser.add_argument(
        '--port', type=int,
        help='Listen port', default=9912)
    parser.add_argument(
        '--interval', type=int,
        help=('Seconds to wait in between publishing '
              'pending proposals'), default=7200)
    parser.add_argument(
        '--no-auto-publish',
        action='store_true',
        help='Do not create merge proposals automatically.')
    parser.add_argument(
        '--config', type=str, default='janitor.conf',
        help='Path to load configuration from.')
    parser.add_argument(
        '--runner-url', type=str, default=None,
        help='URL to reach runner at.')
    parser.add_argument(
        '--slowstart',
        action='store_true', help='Use slow start rate limiter.')
    parser.add_argument(
        '--reviewed-only',
        action='store_true', help='Only publish changes that were reviewed.')
    parser.add_argument(
        '--push-limit', type=int, help='Limit number of pushes per cycle.')
    parser.add_argument(
        '--require-binary-diff', action='store_true', default=False,
        help='Require a binary diff when publishing merge requests.')
    parser.add_argument(
        '--modify-mp-limit', type=int, default=10,
        help='Maximum number of merge proposals to update per cycle.')
    parser.add_argument(
        '--external-url', type=str, help='External URL',
        default='https://janitor.debian.net/')
    parser.add_argument(
        '--differ-url', type=str, help='Differ URL.',
        default='http://localhost:9920/')

    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = read_config(f)

    state.DEFAULT_URL = config.database_location

    if args.slowstart:
        rate_limiter = SlowStartRateLimiter(args.max_mps_per_maintainer)
    elif args.max_mps_per_maintainer > 0:
        rate_limiter = MaintainerRateLimiter(args.max_mps_per_maintainer)
    else:
        rate_limiter = NonRateLimiter()

    if args.no_auto_publish and args.once:
        sys.stderr.write('--no-auto-publish and --once are mutually exclude.')
        sys.exit(1)

    topic_merge_proposal = Topic('merge-proposal')
    topic_publish = Topic('publish')
    loop = asyncio.get_event_loop()
    vcs_manager = LocalVcsManager(config.vcs_location)
    db = state.Database(config.database_location)
    if args.once:
        loop.run_until_complete(publish_pending_new(
            db, rate_limiter, dry_run=args.dry_run,
            external_url=args.external_url,
            differ_url=args.differ_url,
            vcs_manager=vcs_manager, topic_publish=topic_publish,
            topic_merge_proposal=topic_merge_proposal,
            reviewed_only=args.reviewed_only,
            require_binary_diff=args.require_binary_diff))
        if args.prometheus:
            push_to_gateway(
                args.prometheus, job='janitor.publish',
                registry=REGISTRY)
    else:
        tasks = [
            loop.create_task(process_queue_loop(
                db, rate_limiter, dry_run=args.dry_run,
                vcs_manager=vcs_manager, interval=args.interval,
                topic_merge_proposal=topic_merge_proposal,
                topic_publish=topic_publish,
                auto_publish=not args.no_auto_publish,
                external_url=args.external_url,
                differ_url=args.differ_url,
                reviewed_only=args.reviewed_only,
                push_limit=args.push_limit,
                modify_mp_limit=args.modify_mp_limit,
                require_binary_diff=args.require_binary_diff)),
            loop.create_task(
                run_web_server(
                    args.listen_address, args.port, rate_limiter,
                    vcs_manager, db, topic_merge_proposal, topic_publish,
                    dry_run=args.dry_run,
                    external_url=args.external_url,
                    differ_url=args.differ_url,
                    require_binary_diff=args.require_binary_diff,
                    modify_mp_limit=args.modify_mp_limit,
                    push_limit=args.push_limit)),
        ]
        if args.runner_url and not args.reviewed_only:
            tasks.append(loop.create_task(
                listen_to_runner(
                    db, rate_limiter, vcs_manager,
                    args.runner_url, topic_publish,
                    topic_merge_proposal, dry_run=args.dry_run,
                    external_url=args.external_url,
                    differ_url=args.differ_url,
                    require_binary_diff=args.require_binary_diff)))
        loop.run_until_complete(asyncio.gather(*tasks))


if __name__ == '__main__':
    sys.exit(main(sys.argv))
