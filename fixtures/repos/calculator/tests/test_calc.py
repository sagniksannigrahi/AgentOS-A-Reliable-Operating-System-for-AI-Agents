import pytest

import calc


def test_add():
    assert calc.add(2, 3) == 5


def test_subtract():
    assert calc.subtract(5, 3) == 2


def test_multiply():
    assert calc.multiply(4, 2.5) == 10
    assert calc.multiply(-3, 3) == -9


def test_divide():
    assert calc.divide(9, 3) == 3
    assert calc.divide(1, 4) == 0.25


def test_divide_by_zero():
    with pytest.raises(ValueError, match="division by zero"):
        calc.divide(1, 0)
