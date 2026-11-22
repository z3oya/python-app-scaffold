import numpy as np
import pytest

from pyapp import core
from pyapp.app import build_signal


def test_moving_average_shape() -> None:
    signal = build_signal(100, 1)
    smoothed = core.moving_average(signal, 10)
    assert isinstance(smoothed, np.ndarray)
    assert smoothed.shape == (91,)


def test_moving_average_constant() -> None:
    flat = np.full(50, 3.14)
    smoothed = core.moving_average(flat, 5)
    assert np.allclose(smoothed, 3.14)


def test_moving_average_rejects_bad_window() -> None:
    signal = build_signal(10, 1)
    with pytest.raises(ValueError):
        core.moving_average(signal, 0)
    with pytest.raises(ValueError):
        core.moving_average(signal, 11)


def test_summarize() -> None:
    values = np.array([1.0, 2.0, 3.0])
    s = core.summarize(values)
    assert s.count == values.size
    assert s.mean == pytest.approx(float(values.mean()))
    assert s.std == pytest.approx(float(values.std()))
    assert s.peak == float(values.max())


def test_summarize_rejects_empty() -> None:
    with pytest.raises(ValueError):
        core.summarize(np.empty(0))
