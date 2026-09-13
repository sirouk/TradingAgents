"""latest_close vintage rule: reference_close must be as-of the analysis date."""
import io
import importlib.util
import os

import pytest

pytestmark = pytest.mark.unit

import pandas as pd

_spec = importlib.util.spec_from_file_location(
    "tao_verdict_file",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                 "bridge", "tao_verdict.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
latest_close = _mod.latest_close


def _fake_df():
    return pd.DataFrame({
        "Date": ["2025-08-31", "2025-09-01", "2025-09-02", "2025-09-03",
                 "2025-09-04", "2025-09-05", "2025-09-06", "2025-09-07"],
        "Close": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
    })


def _fetch(symbol, start, end):
    records = {"captured": (symbol, start, end)}
    _fetch.captured = records["captured"]
    return _fake_df()


def test_as_of_floors_at_analysis_date():
    got = latest_close("TAO-USD", as_of="2025-09-05", _fetch_klines=_fetch, _resolve=lambda s: s)
    assert got == 15.0                     # the 09-05 close, not a later one
    sym, start, end = _fetch.captured
    assert end == "2025-09-05" and start == "2025-08-29"


def test_as_of_weekend_uses_real_weekend_bar():
    # Saturday + Sunday have genuine bars on a 24/7 market — no carry-forward.
    assert latest_close("TAO-USD", as_of="2025-09-06", _fetch_klines=_fetch, _resolve=lambda s: s) == 16.0
    assert latest_close("TAO-USD", as_of="2025-09-07", _fetch_klines=_fetch, _resolve=lambda s: s) == 17.0


def test_as_of_before_any_bar_returns_none():
    assert latest_close("TAO-USD", as_of="2025-08-01", _fetch_klines=_fetch, _resolve=lambda s: s) is None


def test_realtime_path_unchanged_monkeypatched():
    # None as_of => last bar of the fetch = live behaviour (and end == today).
    got = latest_close("TAO-USD", as_of=None, _fetch_klines=_fetch, _resolve=lambda s: s)
    assert got == 17.0
    from datetime import datetime
    assert _fetch.captured[2] == datetime.utcnow().strftime("%Y-%m-%d")


def test_fetch_failure_returns_none():
    assert latest_close("TAO-USD", as_of="2025-09-05",
                        _fetch_klines=lambda *a: None, _resolve=lambda s: s) is None
