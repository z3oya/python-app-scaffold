"""Application entry: CLI over the business core; --selftest for packaged builds."""

from __future__ import annotations

import argparse

import numpy as np

from pyapp import core


def build_signal(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 10.0 + rng.normal(0, 1.0, n).cumsum()


def report(n: int, window: int, seed: int) -> str:
    signal = build_signal(n, seed)
    smoothed = core.moving_average(signal, window)
    s = core.summarize(smoothed)
    return (
        f"signal {n} pts, window {window}\n"
        f"smoothed: count {s.count}  mean {s.mean:.3f}  "
        f"std {s.std:.3f}  peak {s.peak:.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pyapp")
    ap.add_argument("--points", type=int, default=1000)
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        n_pts, window = 100, 10
        expected = n_pts - window + 1
        signal = build_signal(n_pts, 1)
        smoothed = core.moving_average(signal, window)
        if smoothed.size != expected:
            raise AssertionError(f"smoothed size {smoothed.size} != {expected}")
        s = core.summarize(smoothed)
        if s.count != expected:
            raise AssertionError(f"count {s.count} != {expected}")
        try:
            core.moving_average(signal, 0)
        except ValueError:
            pass
        else:
            raise AssertionError("window=0 must raise")
        print("selftest ok")
        return 0

    print(report(args.points, args.window, args.seed))
    return 0
