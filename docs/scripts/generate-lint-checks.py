#!/usr/bin/env python3

import sys
import textwrap
from typing import List

from tmt.base import Plan, Story, Test
from tmt.lint import Linter
from tmt.utils import Path, render_template_file

HELP = textwrap.dedent("""
Usage: generate-lint-checks.py <TEMPLATE-PATH> <OUTPUT-PATH>

Generate docs for all known lint checks.
""").strip()


def _sort_linters(linters: List[Linter]) -> List[Linter]:
    """ Sort a list of linters by their ID """
    return sorted(linters, key=lambda x: x.id)


def main() -> None:
    if len(sys.argv) != 3:
        print(HELP)

        sys.exit(1)

    template_filepath = Path(sys.argv[1])
    output_filepath = Path(sys.argv[2])

    linters = {
        'TEST_LINTERS': _sort_linters(Test.get_linter_registry()),
        'PLAN_LINTERS': _sort_linters(Plan.get_linter_registry()),
        'STORY_LINTERS': _sort_linters(Story.get_linter_registry()),
        }

    output_filepath.write_text(render_template_file(template_filepath, **linters))


if __name__ == '__main__':
    main()
