#!/usr/bin/env python
"""Run the full Placement Mail Tracker regression test suite.

Usage:
    python run_regression_tests.py
    python run_regression_tests.py -k test_database
    python run_regression_tests.py --tb=short
"""

from __future__ import annotations

import sys

import pytest


def main() -> int:
    """Run the regression suite and return the exit code."""
    default_args = [
        "tests/",
        "-v",
        "--tb=short",
        "-x",  # stop on first failure for quicker feedback
    ]

    # Allow extra CLI args to be appended
    extra = sys.argv[1:]
    args = default_args + extra

    print("=" * 70)
    print("  Placement Mail Tracker – Regression Test Suite")
    print("=" * 70)
    print(f"  Running: pytest {' '.join(args)}")
    print("=" * 70)

    return pytest.main(args)


if __name__ == "__main__":
    sys.exit(main())
