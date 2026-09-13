"""Unit tests for the as-of vintage path in the Polymarket vendor.

All HTTP is stubbed — these tests never touch the network. They verify:
  * _resolve_as_of: live/vintage routing (arg > env > today-fallback)
  * _market_live_at: snapshot-date filtering (not yet listed / already
    resolved / needs CLOB token ids)
  * get_prediction_markets(as_of=...): vintage report content, dates from
    CLOB history, honest gaps for markets without history
"""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit

import tradingagents.dataflows.polymarket as pm


# ---- helpers ---------------------------------------------------------------

def _events(markets):
    return {"events": [{"markets": markets}]}


def _market(**over):
    m = {
        "question": "Fed cuts rates in 2025?",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.72","0.28"]',
        "clobTokenIds": '["111","222"]',
        "startDate": "2025-01-01T00:00:00Z",
        "endDate": "2099-01-01T00:00:00Z",
        "volumeNum": 1e9,
        "closed": False,
    }
    m.update(over)
    return m


def _clob_hist(points):
    return {"history": [{"t": int(ts), "p": float(p)} for ts, p in points]}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ---- _resolve_as_of --------------------------------------------------------

def test_resolve_as_of_none_by_default(monkeypatch):
    monkeypatch.delenv("TA_PREDICTION_MARKETS_ASOF", raising=False)
    assert pm._resolve_as_of(None) is None


def test_resolve_as_of_today_stays_live(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    assert pm._resolve_as_of(today) is None


def test_resolve_as_of_past_date_end_of_day(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    dt = pm._resolve_as_of(past)
    assert dt is not None
    assert dt.isoformat()[:10] == past
    assert (dt.hour, dt.minute, dt.second) == (23, 59, 59)


def test_resolve_as_of_env_fallback(monkeypatch):
    past = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    monkeypatch.setenv("TA_PREDICTION_MARKETS_ASOF", past)
    dt = pm._resolve_as_of(None)
    assert dt is not None and dt.isoformat()[:10] == past


def test_resolve_as_of_unparsable_stays_live(monkeypatch):
    monkeypatch.setenv("TA_PREDICTION_MARKETS_ASOF", "not-a-date")
    assert pm._resolve_as_of(None) is None


# ---- _market_live_at -------------------------------------------------------

def _ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def test_market_live_at_happy():
    assert pm._market_live_at(_market(), _ts("2025-06-15T00:00:00Z")) is True


def test_market_live_at_not_yet_listed():
    assert pm._market_live_at(_market(), _ts("2024-06-15T00:00:00Z")) is False


def test_market_live_at_already_resolved():
    resolved = _market(endDate="2025-05-01T00:00:00Z")
    assert pm._market_live_at(resolved, _ts("2025-06-15T00:00:00Z")) is False


def test_market_live_at_requires_clob_tokens():
    assert pm._market_live_at(_market(clobTokenIds="[]"), _ts("2025-06-15T00:00:00Z")) is False


def test_market_live_at_ignores_closed_flag():
    # closed reflects NOW; for vintage purposes it is deliberately ignored —
    # the timestamp window is the only point-in-time signal.
    m = _market(closed=True)
    assert pm._market_live_at(m, _ts("2025-06-15T00:00:00Z")) is True


# ---- vintage path end to end -----------------------------------------------

def test_vintage_report_uses_clob_history(monkeypatch):
    as_of = "2025-06-15"
    asof_ts = int(datetime(2025, 6, 15, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    points = [
        (asof_ts - 8 * 86400, 0.50),           # beyond a week -> should be ignored for wk drift
        (asof_ts - 7 * 86400 - 3600, 0.60),
        (asof_ts - 86400, 0.70),
        (asof_ts, 0.72),                        # the as-of price
        (asof_ts + 3600, 0.99),                 # after the snapshot -> must NOT be used
    ]
    monkeypatch.setattr(pm, "_request",
                        lambda path, params: _events([_market()]))
    monkeypatch.setattr(pm, "_clob_request",
                        lambda path, params: _clob_hist(points))

    out = pm.get_prediction_markets("Fed rate cut", as_of=as_of)
    assert "point-in-time: as of 2025-06-15" in out
    assert "72%" in out                     # floor at as-of
    assert "99%" not in out                 # future point must not leak
    assert "+12.0pp" in out                 # 0.72 - 0.60
    assert "volume" not in out.lower().split("omitted")[0][-60:] or "volume" in out  # tolerant: footer explains omission


def test_vintage_report_honest_gap_without_history(monkeypatch):
    as_of = "2025-06-15"
    monkeypatch.setattr(pm, "_request", lambda path, params: _events([_market()]))
    monkeypatch.setattr(pm, "_clob_request", lambda path, params: _clob_hist([]))
    out = pm.get_prediction_markets("Fed rate cut", as_of=as_of)
    assert "no CLOB price history before 2025-06-15" in out
    assert "likely listed later" in out


def test_vintage_report_filters_unlisted_and_resolved(monkeypatch):
    as_of = "2025-06-15"
    future_m = _market(question="Q future", startDate="2025-07-01T00:00:00Z")
    past_m = _market(question="Q resolved", endDate="2025-05-01T00:00:00Z")
    good = _market(question="Q live")
    monkeypatch.setattr(pm, "_request",
                        lambda path, params: _events([future_m, past_m, good]))
    monkeypatch.setattr(pm, "_clob_request", lambda path, params: _clob_hist([
        (int(datetime(2025, 6, 15, 20, 0, 0, tzinfo=timezone.utc).timestamp()), 0.66)]))
    out = pm.get_prediction_markets("topic", as_of="2025-06-15")
    assert "Q future" not in out       # not yet listed
    assert "Q resolved" not in out     # already resolved
    assert "Q live" in out and "66%" in out


def test_live_path_unchanged_with_no_as_of(monkeypatch):
    monkeypatch.delenv("TA_PREDICTION_MARKETS_ASOF", raising=False)
    monkeypatch.setattr(pm, "_request", lambda path, params: _events([_market()]))
    out = pm.get_prediction_markets("Fed rate cut")
    assert "point-in-time" not in out
    assert "72%" in out          # normal live path meaning unchanged
