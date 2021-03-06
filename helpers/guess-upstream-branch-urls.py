#!/usr/bin/python3

import argparse
import asyncio
import asyncpg
import sys
from typing import Optional

from google.protobuf import text_format

from janitor import state
from janitor.config import read_config
from janitor import package_overrides_pb2

from lintian_brush.upstream_metadata import (
    guess_from_launchpad,
    guess_from_aur,
    guess_from_pecl,
    )


async def iter_missing_upstream_branch_packages(conn: asyncpg.Connection):
    query = """\
select
  package.name,
  package.archive_version
from
  last_runs
inner join package on last_runs.package = package.name
left outer join upstream on upstream.name = package.name
where
  result_code = 'upstream-branch-unknown' and
  upstream.upstream_branch_url is null
order by package.name asc
"""
    for row in await conn.fetch(query):
        yield row[0], row[1]


async def main(db, start=None):
    async with db.acquire() as conn:
        async for pkg, version in iter_missing_upstream_branch_packages(conn):
            if start and pkg < start:
                continue
            sys.stderr.write('Package: %s\n' % pkg)
            urls = []
            for name, guesser in [
                    ('aur', guess_from_aur),
                    ('lp', guess_from_launchpad),
                    ('pecl', guess_from_pecl)]:
                metadata = dict(guesser(pkg))
                try:
                    repo_url = metadata['Repository']
                except KeyError:
                    continue
                else:
                    urls.append((name, repo_url))
            if not urls:
                continue
            if len(urls) > 1:
                print('# Note: Conflicting URLs for %s: %r' % (pkg, urls))
            config = package_overrides_pb2.OverrideConfig()
            override = config.package.add()
            override.name = pkg
            override.upstream_branch_url = urls[0][1]
            print("# From %s" % urls[0][0])
            text_format.PrintMessage(config, sys.stdout)

parser = argparse.ArgumentParser('guess-upstream-branch-urls')
parser.add_argument(
    '--config', type=str, default='janitor.conf',
    help='Path to configuration.')
parser.add_argument(
    '--start', type=str, default='',
    help='Only process package with names after this one.')

args = parser.parse_args()
with open(args.config, 'r') as f:
    config = read_config(f)

db = state.Database(config.database_location)
asyncio.run(main(db, args.start))
