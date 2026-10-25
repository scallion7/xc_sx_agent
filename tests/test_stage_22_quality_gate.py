"""Compatibility entry point; maintained tests live in tests.test_operations_quality."""

from tests import test_operations_quality as _suite


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
