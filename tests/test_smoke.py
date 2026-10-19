"""Smoke test: the package imports and the test runner is wired up.

Author: Rajveer Sharma
Date: 18 Oct 2026
"""

import ott_agent


def test_package_imports():
    assert ott_agent.__version__
