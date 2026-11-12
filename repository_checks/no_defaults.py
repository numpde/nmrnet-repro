"""Run all silent-default checks, refusing an empty discovery."""

from tools.container_guard import require_container

require_container()

import unittest


def load_checks(loader):
    checks = loader.discover("repository_checks/quality/silent_defaults", top_level_dir=".")
    if checks.countTestCases() == 0:
        raise ValueError("Cannot run silent-default checks: discovery found no tests.")
    checks.addTests(
        loader.loadTestsFromName(
            "repository_checks.quality.test_make_hygiene.RepositoryHygieneTest.test_private_archive_handoff_keeps_its_empty_unexported_baseline"
        )
    )
    checks.addTests(loader.loadTestsFromName("repository_checks.test_no_defaults_lane"))
    return checks


def main():
    result = unittest.TextTestRunner(verbosity=2).run(load_checks(unittest.TestLoader()))
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
