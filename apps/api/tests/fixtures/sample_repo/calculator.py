"""Simple calculator module.

BUG: The ``add`` function currently subtracts instead of adds.
This is the intentional defect used for coding-agent integration tests.
"""


def add(a: int, b: int) -> int:
    """Return the sum of *a* and *b*.

    BUG: currently returns a - b instead of a + b.
    """
    return a - b  # BUG: should be a + b


def subtract(a: int, b: int) -> int:
    """Return the difference of *a* and *b*."""
    return a - b


def multiply(a: int, b: int) -> int:
    """Return the product of *a* and *b*."""
    return a * b


def divide(a: int, b: int) -> float:
    """Return *a* divided by *b*.

    Raises ZeroDivisionError if *b* is zero.
    """
    if b == 0:
        raise ZeroDivisionError("Cannot divide by zero")
    return a / b
