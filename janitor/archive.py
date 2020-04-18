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

from aiohttp import ClientSession
import asyncio
from contextlib import ExitStack
import os
import re
import sys
import tempfile
import uuid

from aiohttp import web
from debian.deb822 import Changes

from .debdiff import (
    run_debdiff,
    DebdiffError,
    filter_boring as filter_debdiff_boring,
    htmlize_debdiff,
    markdownify_debdiff,
    )
from .diffoscope import (
    filter_boring as filter_diffoscope_boring,
    filter_irrelevant as filter_diffoscope_irrelevant,
    run_diffoscope,
    format_diffoscope,
    )
from .prometheus import setup_metrics
from .trace import note

suite_check = re.compile('^[a-z0-9-]+$')


async def handle_upload(request):
    reader = await request.multipart()
    filenames = []
    while True:
        part = await reader.next()
        if part is None:
            break
        path = os.path.join(request.app.incoming_dir, part.filename)
        filenames.append(part.filename)
        with open(path, 'wb') as f:
            f.write(await part.read(decode=False))
    note('Uploaded files: %r', filenames)
    return web.Response(status=200, text='Uploaded files: %r.' % filenames)


def find_changes_path(incoming_dir, archive_dir, suite, changes_filename):
    for path in [
            os.path.join(incoming_dir, changes_filename),
            os.path.join(archive_dir, suite, changes_filename)]:
        if os.path.exists(path):
            return path
    else:
        return None


async def handle_debdiff(request):
    post = await request.post()

    old_suite = post.get('old_suite', 'unchanged')
    if not suite_check.match(old_suite):
        return web.Response(
            status=400, text='Invalid old suite %s' % old_suite)

    try:
        new_suite = post['new_suite']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: new_suite')

    if not suite_check.match(new_suite):
        return web.Response(
            status=400, text='Invalid new suite %s' % new_suite)

    try:
        old_changes_filename = post['old_changes_filename']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: old_changes_filename')

    if '/' in old_changes_filename:
        return web.Response(
            status=400,
            text='Invalid changes filename: %s' % old_changes_filename)

    try:
        new_changes_filename = post['new_changes_filename']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: new_changes_filename')

    if '/' in new_changes_filename:
        return web.Response(
            status=400,
            text='Invalid changes filename: %s' % new_changes_filename)

    archive_path = request.app.archive_path

    old_changes_path = find_changes_path(
            request.app.incoming_dir, archive_path, old_suite,
            old_changes_filename)

    if old_changes_path is None:
        return web.Response(
            status=404, text='Old changes file %s does not exist.' % (
                old_changes_filename))

    new_changes_path = find_changes_path(
            request.app.incoming_dir, archive_path, new_suite,
            new_changes_filename)

    if new_changes_path is None:
        return web.Response(
            status=404, text='New changes file %s does not exist.' % (
                new_changes_filename))

    with open(old_changes_path, 'r') as f:
        old_changes = Changes(f)

    with open(new_changes_path, 'r') as f:
        new_changes = Changes(f)

    try:
        debdiff = await run_debdiff(old_changes_path, new_changes_path)
    except DebdiffError as e:
        return web.Response(status=400, text=e.args[0])

    if 'filter_boring' in post:
        debdiff = filter_debdiff_boring(
            debdiff.decode(), old_changes['Version'],
            new_changes['Version']).encode()

    for accept in request.headers.get('ACCEPT', '*/*').split(','):
        if accept in ('text/x-diff', 'text/plain', '*/*'):
            return web.Response(
                body=debdiff,
                content_type='text/plain')
        if accept == 'text/markdown':
            return web.Response(
                text=markdownify_debdiff(debdiff.decode('utf-8', 'replace')),
                content_type='text/markdown')
        if accept == 'text/html':
            return web.Response(
                text=htmlize_debdiff(debdiff.decode('utf-8', 'replace')),
                content_type='text/html')
    raise web.HTTPNotAcceptable(
        text='Acceptable content types: '
             'text/html, text/plain, text/markdown')


async def handle_diffoscope(request):
    post = await request.post()

    old_suite = post.get('old_suite', 'unchanged')
    if not suite_check.match(old_suite):
        return web.Response(
            status=400, text='Invalid old suite %s' % old_suite)

    try:
        new_suite = post['new_suite']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: new_suite')

    if not suite_check.match(new_suite):
        return web.Response(
            status=400, text='Invalid new suite %s' % new_suite)

    try:
        old_changes_filename = post['old_changes_filename']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: old_changes_filename')

    if '/' in old_changes_filename:
        return web.Response(
            status=400,
            text='Invalid changes filename: %s' % old_changes_filename)

    try:
        new_changes_filename = post['new_changes_filename']
    except KeyError:
        return web.Response(
            status=400, text='Missing argument: new_changes_filename')

    if '/' in new_changes_filename:
        return web.Response(
            status=400,
            text='Invalid changes filename: %s' % new_changes_filename)

    archive_path = request.app.archive_path

    old_changes_path = find_changes_path(
            request.app.incoming_dir, archive_path, old_suite,
            old_changes_filename)

    if old_changes_path is None:
        return web.Response(
            status=404, text='Old changes file %s does not exist.' % (
                old_changes_filename))

    new_changes_path = find_changes_path(
            request.app.incoming_dir, archive_path, new_suite,
            new_changes_filename)

    if new_changes_path is None:
        return web.Response(
            status=404, text='New changes file %s does not exist.' % (
                new_changes_filename))

    for accept in request.headers.get('ACCEPT', '*/*').split(','):
        if accept in ('text/plain', '*/*'):
            content_type = 'text/plain'
            break
        elif accept in ('text/html', ):
            content_type = 'text/html'
            break
        elif accept in ('application/json', ):
            content_type = 'application/json'
            break
        elif accept in ('text/markdown', ):
            content_type = 'text/markdown'
            break
    else:
        raise web.HTTPNotAcceptable(
            text='Acceptable content types: '
                 'text/html, text/plain, application/json, '
                 'application/markdown')

    with open(old_changes_path, 'r') as f:
        old_changes = Changes(f)

    with open(new_changes_path, 'r') as f:
        new_changes = Changes(f)

    def set_limits():
        import resource
        # Limit to 200Mb
        resource.setrlimit(
            resource.RLIMIT_AS, (200 * 1024 * 1024, 200 * 1024 * 1024))

    diffoscope_diff = await run_diffoscope(
        old_changes_path, new_changes_path,
        set_limits)

    filter_diffoscope_irrelevant(diffoscope_diff)

    title = 'diffoscope for %s applied to %s' % (
         new_changes['Distribution'], new_changes['Source'])

    if 'filter_boring' in post:
        filter_diffoscope_boring(
            diffoscope_diff, old_changes['Version'],
            new_changes['Version'], old_changes['Distribution'],
            new_changes['Distribution'])
        title += ' (filtered)'

    debdiff = await format_diffoscope(
        diffoscope_diff, content_type,
        title=title)

    return web.Response(text=debdiff, content_type=content_type)


async def handle_old_archive_file(request):
    filename = request.match_info['filename']
    if '/' in filename:
        return web.Response(
            text='Invalid filename %s' % request.match_info['filename'],
            status=400)

    full_path = os.path.join(
        request.app.old_archive_path,
        request.match_info['suite'],
        filename)

    if os.path.exists(full_path):
        return web.FileResponse(full_path)
    else:
        return web.Response(
            text='No such changes file : %s' % filename, status=404)


async def handle_archive_dist_file(request):
    args = ['dists', request.match_info['suite']]
    if request.match_info.get('component'):
        args.append(request.match_info['component'])
        if request.match_info.get('arch'):
            args.append(request.match_info['arch'])
    args.append(request.match_info['filename'])
    full_path = os.path.join(
        request.app.archive_path, *args)

    if os.path.exists(full_path):
        return web.FileResponse(full_path)
    else:
        return web.Response(
            text='No such archive file : %s' % '/'.join(args), status=404)


async def handle_archive_pool_file(request):
    args = ['pool',
        request.match_info['component'],
        request.match_info['prefix'],
        request.match_info['package'],
        request.match_info['filename']]

    full_path = os.path.join(request.app.archive_path, *args)

    if os.path.exists(full_path):
        return web.FileResponse(full_path)
    else:
        return web.Response(
            text='No such archive pool file : %s' % '/'.join(arsg), status=404)


async def run_web_server(listen_addr, port, archive_path, old_archive_path, incoming_dir):
    app = web.Application()
    app.archive_path = archive_path
    app.old_archive_path = old_archive_path
    app.incoming_dir = incoming_dir
    setup_metrics(app)
    app.router.add_post('/', handle_upload, name='upload')
    app.router.add_post('/debdiff', handle_debdiff, name='debdiff')
    app.router.add_post('/diffoscope', handle_diffoscope, name='diffoscope')
    app.router.add_get(
        '/archive'
        '/{suite:[a-z0-9-]+}'
        '/{filename}', handle_old_archive_file, name='old-archive-file')
    app.router.add_get(
        '/dists'
        '/{suite:[a-z0-9-]+}'
        '/{filename:[^/]+}', handle_archive_dist_file, name='dist-file')
    app.router.add_get(
        '/dists'
        '/{suite:[a-z0-9-]+}'
        '/{component:[a-z]+}'
        '/{filename:[^/]+}', handle_archive_dist_file,
        name='dist-component-file')
    app.router.add_get(
        '/dists'
        '/{suite:[a-z0-9-]+}'
        '/{component:[a-z]+}'
        '/{arch:[a-z0-9]-]+}'
        '/{filename:[^/]+}', handle_archive_dist_file,
        name='dist-component-arch-file')
    app.router.add_get(
        '/pool'
        '/{component:[a-z]+}'
        '/{prefix:[a-z0-9]-]+}'
        '/{package:[a-z0-9]-}+'
        '/{filename:[^/]+}', handle_archive_pool_file, name='pool-file')
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, listen_addr, port)
    await site.start()


async def update_aptly(incoming_dir, remove_files=True):
    args = [
        "/usr/bin/aptly", "repo", "include",
        "-keyring=/home/janitor/debian-janitor/janitor.gpg"]
    if not remove_files:
        args.append("-no-remove-files")
    args.append(incoming_dir)
    proc = await asyncio.create_subprocess_exec(*args)
    await proc.wait()


async def update_mini_dinstall(config, archive_dir):
    with tempfile.NamedTemporaryFile(mode='w') as f:
        with open('mini-dinstall.conf', 'r') as t:
            f.write(t.read() % {'archive_dir': archive_dir})
        for suite in config.suite:
            f.write('[%s]\n' % suite.name)
            f.write('experimental_release = 1\n')
            f.write('release_label = %s\n' % suite.archive_description)
            f.write('\n')

        f.flush()

        args = ['mini-dinstall', '-b', '-c', f.name]
        proc = await asyncio.create_subprocess_exec(*args)
        await proc.wait()


async def update_archive_loop(config, old_archive_dir, incoming_dir):
    while True:
        await update_aptly(incoming_dir, remove_files=False)
        await update_mini_dinstall(config, old_archive_dir)
        await asyncio.sleep(30 * 60)


def main(argv=None):
    import argparse
    from .config import read_config
    parser = argparse.ArgumentParser(prog='janitor.runner')
    parser.add_argument(
        '--listen-address', type=str,
        help='Listen address', default='localhost')
    parser.add_argument(
        '--port', type=int,
        help='Listen port', default=9914)
    parser.add_argument(
        '--archive', type=str,
        help='Path to the apt archive.')
    parser.add_argument(
        '--old-archive', type=str,
        help='Path to the old apt archive.')
    parser.add_argument(
        '--config', type=str, default='janitor.conf',
        help='Path to configuration.')
    parser.add_argument(
        '--incoming', type=str,
        help='Path to incoming directory.')

    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = read_config(f)

    # TODO(jelmer): Run aptly repo create for every configuration suite
    # TODO(jelmer): Run aptly publish repo for every configuration suite

    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(
        loop.create_task(run_web_server(
            args.listen_address, args.port, args.archive, args.old_archive,
            args.incoming)),
        loop.create_task(update_archive_loop(
            config, args.old_archive, args.incoming))))
    loop.run_forever()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
