"""Entry point for the test suite: `python run_tests.py`."""

import sys

from tests import runner

MODULE_NAMES = [
    "tests.test_lexer",
    "tests.test_parser",
    "tests.test_model",
    "tests.test_expr",
    "tests.test_tsdb",
    "tests.test_relational",
    "tests.test_planner",
    "tests.test_session",
    "tests.test_migrate",
    "tests.test_oracle",
]


def main(argv):
    only = [a for a in argv if not a.startswith("-")]
    verbose = "-v" in argv
    names = MODULE_NAMES
    if only:
        names = [n for n in names if any(o in n for o in only)]
    modules = []
    for name in names:
        try:
            modules.append(__import__(name, fromlist=["*"]))
        except ImportError as e:
            print("skipping %s (%s)" % (name, e))
    return runner.run(modules, verbose=verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
