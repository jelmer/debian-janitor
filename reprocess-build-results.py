#!/usr/bin/python3

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import silver_platter  # noqa: E402, F401
from janitor import state  # noqa: E402
from janitor.config import read_config
from janitor.logs import get_log_manager  # noqa: E402
from janitor.sbuild_log import worker_failure_from_sbuild_log  # noqa: E402
from janitor.trace import note  # noqa: E402


loop = asyncio.get_event_loop()

parser = argparse.ArgumentParser()
parser.add_argument(
    '--config', type=str, default='janitor.conf',
    help='Path to configuration.')
args = parser.parse_args()


with open(args.config, 'r') as f:
    config = read_config(args.config)


logfile_manager = get_log_manager(config.logs_location)


async def reprocess_run(package, log_id, result_code, description):
    try:
        build_logf = await logfile_manager.get_log(
            package, log_id, 'build.log')
    except FileNotFoundError:
        return
    failure = worker_failure_from_sbuild_log(build_logf)
    if failure.error:
        new_code = '%s-%s' % (failure.stage, failure.error.kind)
    elif failure.stage:
        new_code = 'build-failed-stage-%s' % failure.stage
    else:
        new_code = 'build-failed'
    if new_code != result_code or description != failure.description:
        await state.update_run_result(log_id, new_code, failure.description)
        note('Updated %r, %r => %r, %r', result_code, description,
             new_code, failure.description)


async def process_all_build_failures():
    todo = []
    async for package, log_id, result_code, description in (
            state.iter_build_failures()):
        todo.append(reprocess_run(package, log_id, result_code, description))
    for i in range(0, len(todo), 100):
        await asyncio.gather(*todo[i:i+100])


loop.run_until_complete(process_all_build_failures())
