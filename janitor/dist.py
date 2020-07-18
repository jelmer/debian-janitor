#!/usr/bin/python3
# Copyright (C) 2020 Jelmer Vernooij <jelmer@jelmer.uk>
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

from breezy.export import export
from debian.deb822 import Deb822
from janitor.schroot import Session
from janitor.trace import note
import os
import shutil
import stat
import tempfile
from breezy.plugins.debian.repack_tarball import get_filetype


def apt_install(session, packages):
    session.check_call(
        ['apt', '-y', 'install'] + packages, cwd='/', user='root')


def apt_satisfy(session, deps):
    session.check_call(
        ['apt', '-y', 'satisfy'] + deps, cwd='/', user='root')


def satisfy_build_deps(session, tree):
    source = Deb822(tree.get_file('debian/control'))
    deps = []
    for name in ['Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch']:
        try:
            deps.append(source[name])
        except KeyError:
            pass
    for name in ['Build-Conflicts', 'Build-Conflicts-Indeo',
                 'Build-Conflicts-Arch']:
        try:
            deps.append('Conflicts: ' + source[name])
        except KeyError:
            pass
    apt_satisfy(session, deps)


def run_dist_in_chroot(session):
    if os.path.exists('package.xml'):
        apt_install(session, ['php-pear', 'php-horde-core'])
        note('Found package.xml, assuming pear package.')
        session.check_call(['pear', 'package'])
        return

    if os.path.exists('pyproject.toml'):
        note('Found pyproject.toml, assuming poetry project.')
        import toml
        with open('pyproject.toml', 'r') as pf:
            pyproject = toml.load(pf)
        if 'poetry' in pyproject.get('tool', []):
            apt_install(session, ['python3-venv', 'python3-pip'])
            session.check_call(['pip3', 'install', 'poetry'])
            session.check_call(['poetry', 'build', '-f', 'sdist'])
            return

    if os.path.exists('setup.py'):
        note('Found setup.py, assuming python project.')
        apt_install(
            session, [
                'python3', 'python3-setuptools',
                'python3-setuptools-scm'])
        if os.stat('setup.py').st_mode & stat.S_IEXEC:
            apt_install(session, ['python'])
            session.check_call(['./setup.py', 'sdist'])
        else:
            session.check_call(['python3', './setup.py', 'sdist'])
        return

    if os.path.exists('dist.ini') and not os.path.exists('Makefile.PL'):
        apt_install(session, ['libdist-inkt-perl'])
        with open('dist.ini', 'rb') as f:
            for line in f:
                if not line.startswith(b';;'):
                    continue
                try:
                    (key, value) = line[2:].split(b'=', 1)
                except ValueError:
                    continue
                if (key.strip() == b'class' and
                        value.strip().startswith(b"'Dist::Inkt")):
                    note('Found Dist::Inkt section in dist.ini, '
                         'assuming distinkt.')
                    # TODO(jelmer): install via apt if possible
                    session.check_call(
                        ['cpan', 'install', value.decode().strip("'")])
                    session.check_call(['distinkt-dist'])
        # Default to invoking Dist::Zilla
        note('Found dist.ini, assuming dist-zilla.')
        session.check_call(['dzil', 'build', '--in', '..'])
        return

    if os.path.exists('Makefile.PL') and not os.path.exists('Makefile'):
        apt_install(session, ['perl'])
        session.check_call(['perl', 'Makefile.PL'])

    if os.path.exists('Makefile.PL'):
        apt_install(session, ['make'])
        session.check_call(['make', 'dist'])
        return

    raise Exception('no known build tools found')


def create_dist_schroot(
        tree, subdir, target_filename, packaging_tree, chroot,
        include_controldir=True):
    if subdir is None:
        subdir = 'package'
    with Session(chroot) as session:
        if packaging_tree is not None:
            satisfy_build_deps(session, packaging_tree)
        build_dir = os.path.join(session.location, 'build')

        directory = tempfile.mkdtemp(dir=build_dir)
        reldir = '/' + os.path.relpath(directory, session.location)

        export_directory = os.path.join(directory, subdir)
        if not include_controldir:
            export(tree, export_directory, 'dir', subdir)
        else:
            tree.branch.controldir.sprout(
                export_directory,
                create_tree_if_local=True,
                source_branch=tree.branch)

        existing_files = os.listdir(export_directory)

        oldcwd = os.getcwd()
        os.chdir(export_directory)
        try:
            session.chdir(os.path.join(reldir, subdir))
            run_dist_in_chroot(session)
        finally:
            os.chdir(oldcwd)

        new_files = os.listdir(export_directory)
        diff_files = set(new_files) - set(existing_files)
        diff = [n for n in diff_files if get_filetype(n) is not None]
        if len(diff) == 1:
            note('Found tarball %s in package directory.', diff[0])
            shutil.copy(
                os.path.join(export_directory, diff[0]),
                target_filename)
            return True
        if 'dist' in diff_files:
            for entry in os.scandir(os.path.join(export_directory, 'dist')):
                if get_filetype(entry.name) is not None:
                    note('Found tarball %s in dist directory.', entry.name)
                    shutil.copy(entry.path, target_filename)
                    return True
            note('No tarballs found in dist directory.')

        diff = set(os.listdir(directory)) - set([subdir])
        if len(diff) == 1:
            fn = diff.pop()
            note('Found tarball %s in parent directory.', fn)
            shutil.copy(
                os.path.join(directory, fn),
                target_filename)
            return True

        note('No tarball created :(')
        return False


if __name__ == '__main__':
    import argparse
    from breezy.workingtree import WorkingTree
    import breezy.bzr
    import breezy.git
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--chroot', default='unstable-amd64-sbuild', type=str,
        help='Name of chroot to use')
    parser.add_argument(
        'directory', default='.', type=str,
        help='Directory with upstream source.')
    parser.add_argument(
        '--packaging-directory', type=str,
        help='Path to packaging directory.')
    parser.add_argument(
        '--target-filename', type=str,
        help='Target filename')
    args = parser.parse_args()
    tree = WorkingTree.open(args.directory)
    if args.packaging_directory:
        packaging_tree = WorkingTree.open(args.packaging_directory)
        with packaging_tree.lock_read():
            source = Deb822(packaging_tree.get_file('debian/control'))
        package = source['Source']
        subdir = package
        target_filename = args.target_filename or ('%s.tar.gz' % package)
    else:
        packaging_tree = None
        target_filename = args.target_filename or 'dist.tar.gz'
        subdir = None

    create_dist_schroot(
        tree, subdir, os.path.abspath(target_filename), packaging_tree,
        args.chroot)