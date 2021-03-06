#!/usr/bin/python3

import sys

import breezy
import breezy.plugins
import breezy.bzr
import breezy.git  # noqa: F401

from breezy.propose import (
    hosters,
    )
import breezy.plugins.gitlab  # noqa: F401
import breezy.plugins.github  # noqa: F401
import breezy.plugins.launchpad  # noqa: F401


def projects_to_remove(instance):
    in_use = set()
    for mp in instance.iter_my_proposals():
        if not mp.is_closed() and not mp.is_merged():
            in_use.add(mp.get_source_project())
    for project in instance.iter_my_forks():
        if project in in_use:
            continue
        yield project


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help="Dry run.")
    args = parser.parse_args()

    for name, hoster_cls in hosters.items():
        for instance in hoster_cls.iter_instances():
            for project in projects_to_remove(instance):
                print('Deleting %s from %r' % (project, instance))
                if not args.dry_run:
                    instance.delete_project(project)


if __name__ == '__main__':
    sys.exit(main())
