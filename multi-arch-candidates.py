#!/usr/bin/python3

from janitor.candidates_pb2 import Candidate, CandidateList


async def iter_multiarch_candidates(packages=None):
    from silver_platter.debian.multiarch import (
        calculate_value,
        )
    from lintian_brush.multiarch_hints import (
        download_multiarch_hints,
        parse_multiarch_hints,
        multiarch_hints_by_source,
        )
    with download_multiarch_hints() as f:
        hints = parse_multiarch_hints(f)
        bysource = multiarch_hints_by_source(hints)
    for source, entries in bysource.items():
        if packages is not None and source not in packages:
            continue
        hints = [entry['link'].rsplit('#', 1)[-1] for entry in entries]
        value = calculate_value(hints)
        candidate = Candidate()
        candidate.package = source
        candidate.context = ' '.join(sorted(hints))
        candidate.value = value
        yield candidate


async def main():
    import argparse

    parser = argparse.ArgumentParser(prog='multi-arch-candidates')
    parser.add_argument("packages", nargs='*', default=None)

    args = parser.parse_args()

    async for candidate in iter_multiarch_candidates(
            args.packages or None):
        cl = CandidateList()
        cl.candidate.append(candidate)
        print(cl)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
