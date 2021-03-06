#!/usr/bin/python3

from janitor import state


async def write_merge_proposals(db, suite):
    async with db.acquire() as conn:
        proposals_by_status = {}
        async for run, url, status in state.iter_proposals_with_run(
                conn, suite=suite):
            proposals_by_status.setdefault(status, []).append((url, run))

    merged = (proposals_by_status.get('merged', []) +
              proposals_by_status.get('applied', []))
    return {
            'suite': suite,
            'open_proposals': proposals_by_status.get('open', []),
            'merged_proposals': merged,
            'closed_proposals': proposals_by_status.get('closed', []),
            }
