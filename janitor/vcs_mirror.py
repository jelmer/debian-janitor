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

"""Import VCS branches."""

from breezy import urlutils

from datetime import datetime, timedelta

import sys
import time
import urllib.parse

from prometheus_client import (
    Gauge,
    REGISTRY,
    push_to_gateway,
    )

from breezy.plugins.propose.gitlabs import connect_gitlab

from . import state
from .vcs import (
    open_branch_ext,
    BranchOpenFailure,
    mirror_branches,
    )
from .trace import note


last_success_gauge = Gauge(
    'job_last_success_unixtime',
    'Last time a batch job successfully finished')


def update_gitlab_branches(vcs_result_dir, host):
    package_per_repo = {}
    branches_per_repo = {}
    for name, branch_url, revision in state.iter_package_branches():
        if branch_url.startswith('https://%s/' % host):
            url, params = urlutils.split_segment_parameters(branch_url)
            branches_per_repo.setdefault(url, {})
            branches_per_repo[url][params.get('branch')] = revision
            package_per_repo[url] = name

    salsa = connect_gitlab(host)
    for project in salsa.projects.list(
            order_by='updated_at', archived=False, visibility='public',
            as_list=False):
        if (datetime.now() -
                datetime.fromisoformat(project.last_activity_at[:-1])
                > timedelta(days=5)):
            break
        for branch_name, last_revision in branches_per_repo.get(
                project.http_url_to_repo, {}).items():
            if branch_name is None:
                branch = project.branches.get(project.default_branch)
            else:
                branch = project.branches.get(branch_name)
            commit_id = branch.commit['id']
            revision = 'git-v1:%s' % commit_id
            if revision == last_revision:
                continue
            if branch_name:
                url = '%s,branch=%s' % (project.http_url_to_repo, branch_name)
            else:
                url = project.http_url_to_repo
            note('Updating %s (last activity: %s)', url,
                 project.last_activity_at)
            suite = 'master'
            try:
                branch = open_branch_ext(url)
            except BranchOpenFailure as e:
                state.update_branch_status(
                    url, last_scanned=datetime.now(), status=e.code,
                    revision=None, description=e.description)
            else:
                mirror_branches(
                    vcs_result_dir, package_per_repo[project.http_url_to_repo],
                    [(suite, branch)], public_master_branch=branch)

                state.update_branch_status(
                    url, last_scanned=datetime.now(), status='success',
                    revision=revision.encode('utf-8'))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='janitor.vcs_mirror')
    parser.add_argument(
        '--prometheus', type=str,
        help='Prometheus push gateway to export to.')
    parser.add_argument(
        '--vcs-result-dir', type=str,
        help='Directory to store VCS repositories in.',
        default='vcs')
    parser.add_argument(
        '--delay', type=int,
        help='Number of seconds to wait in between repositories.',
        default=None)

    args = parser.parse_args()

    prefetch_hosts = ['salsa.debian.org']
    for host in prefetch_hosts:
        update_gitlab_branches(args.vcs_result_dir, host)

    for package, suite, branch_url, last_scanned in (
            state.iter_unscanned_branches(
                last_scanned_minimum=timedelta(days=7))):
        note('Processing %s', package)
        netloc = urllib.parse.urlparse(branch_url).netloc
        # TODO(jelmer): scan prefetch hosts too, just after a much longer
        # period (1 month?)
        if netloc in prefetch_hosts and last_scanned:
            continue
        try:
            branch = open_branch_ext(branch_url)
        except BranchOpenFailure as e:
            state.update_branch_status(
                branch_url, last_scanned=datetime.now(), status=e.code,
                revision=None, description=e.description)
        else:
            mirror_branches(
                args.vcs_result_dir, package, [(suite, branch)],
                public_master_branch=branch)
            state.update_branch_status(
                branch_url, last_scanned=datetime.now(), status='success',
                revision=branch.last_revision())
        if args.delay:
            time.sleep(args.delay)

    last_success_gauge.set_to_current_time()
    if args.prometheus:
        push_to_gateway(
            args.prometheus, job='janitor.vcs_mirror',
            registry=REGISTRY)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
