"""Compatibility entry point; maintained tests live in tests.test_plan_execution."""

from tests import test_plan_execution as _suite


def __getattr__(name):
    return getattr(_suite, name)


def load_tests(loader, tests, pattern):
    # Discovery also visits the canonical module; avoid counting each test twice.
    if pattern is not None:
        return tests
    return loader.loadTestsFromModule(_suite)


if __name__ == "__main__":
    import unittest

    unittest.main()
