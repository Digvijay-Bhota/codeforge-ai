"""Tests for the calculator module.

test_add WILL FAIL until the bug in calculator.add() is fixed.
All other tests pass with the original (buggy) code.
"""

import pytest
from calculator import add, divide, multiply, subtract


def test_add() -> None:
    """This test fails while the bug is present (add returns a - b)."""
    assert add(2, 3) == 5
    assert add(0, 0) == 0
    assert add(-1, 1) == 0


def test_subtract() -> None:
    assert subtract(5, 3) == 2
    assert subtract(0, 5) == -5


def test_multiply() -> None:
    assert multiply(3, 4) == 12
    assert multiply(-2, 5) == -10


def test_divide() -> None:
    assert divide(10, 2) == 5.0
    assert divide(7, 2) == 3.5


def test_divide_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
