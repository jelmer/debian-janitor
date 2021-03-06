#!/usr/bin/python
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

import datetime
from janitor.build import add_dummy_changelog_entry

from breezy.tests import TestCaseWithTransport


class AddDummyChangelogEntryTests(TestCaseWithTransport):

    def test_simple(self):
        tree = self.make_branch_and_tree('.')
        self.build_tree_contents([('debian/', ), ('debian/changelog', """\
janitor (0.1-1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""")])
        tree.add(['debian', 'debian/changelog'])
        add_dummy_changelog_entry(
            tree, '', 'jan+some', 'some-fixes', 'Dummy build.',
            timestamp=datetime.datetime(2020, 9, 5, 12, 35, 4, 899654),
            maintainer=("Jelmer Vernooĳ", "jelmer@debian.org"))
        self.assertFileEqual("""\
janitor (0.1-1jan+some1) some-fixes; urgency=low

  * Dummy build.

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 05 Sep 2020 12:35:04 -0000

janitor (0.1-1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""", 'debian/changelog')

    def test_native(self):
        tree = self.make_branch_and_tree('.')
        self.build_tree_contents([('debian/', ), ('debian/changelog', """\
janitor (0.1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""")])
        tree.add(['debian', 'debian/changelog'])
        add_dummy_changelog_entry(
            tree, '', 'jan+some', 'some-fixes', 'Dummy build.',
            timestamp=datetime.datetime(2020, 9, 5, 12, 35, 4, 899654),
            maintainer=("Jelmer Vernooĳ", "jelmer@debian.org"))
        self.assertFileEqual("""\
janitor (0.1jan+some1) some-fixes; urgency=low

  * Dummy build.

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 05 Sep 2020 12:35:04 -0000

janitor (0.1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""", 'debian/changelog')

    def test_exists(self):
        tree = self.make_branch_and_tree('.')
        self.build_tree_contents([('debian/', ), ('debian/changelog', """\
janitor (0.1-1jan+some1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""")])
        tree.add(['debian', 'debian/changelog'])
        add_dummy_changelog_entry(
            tree, '', 'jan+some', 'some-fixes', 'Dummy build.',
            timestamp=datetime.datetime(2020, 9, 5, 12, 35, 4, 899654),
            maintainer=("Jelmer Vernooĳ", "jelmer@debian.org"))
        self.assertFileEqual("""\
janitor (0.1-1jan+some2) some-fixes; urgency=low

  * Dummy build.

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 05 Sep 2020 12:35:04 -0000

janitor (0.1-1jan+some1) UNRELEASED; urgency=medium

  * Initial release. (Closes: #XXXXXX)

 -- Jelmer Vernooĳ <jelmer@debian.org>  Sat, 04 Apr 2020 14:12:13 +0000
""", 'debian/changelog')
