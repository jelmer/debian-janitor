#!/usr/bin/python3

from . import state
from .common import generate_pkg_context


SUITE = 'cme'


async def generate_candidates(db):
    candidates = []
    async with db.acquire() as conn:
        for (package, suite, context, value,
             success_chance) in await state.iter_candidates(conn, suite=SUITE):
            candidates.append((package.name, value))
        candidates.sort(key=lambda x: x[1], reverse=True)
    return {'candidates': candidates}


async def generate_pkg_file(db, config, policy, client, archiver_url,
                            publisher_url, package, run_id=None):
    return await generate_pkg_context(
        db, config, SUITE, policy, client, archiver_url, publisher_url,
        package, run_id=run_id)