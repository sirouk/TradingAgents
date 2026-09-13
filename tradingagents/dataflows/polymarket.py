"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""
import json
import os
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# The backfill lane pins odds at a past date through this env var (ISO date
# or datetime; a bare date means end-of-day UTC). It can also be passed as
# the ``as_of`` argument on ``get_prediction_markets``.
ASOF_ENV_VAR = "TA_PREDICTION_MARKETS_ASOF"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    response = requests.get(
        f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def _clob_request(path: str, params: dict) -> dict:
    """Same shape as :func:`_request` but against the public CLOB API."""
    response = requests.get(
        f"{CLOB_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _parse_dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_as_of(as_of) -> datetime | None:
    """Resolve the as-of pin to a datetime; ``None`` means live (today).

    Chain: explicit argument, then the ``TA_PREDICTION_MARKETS_ASOF`` env var
    (ISO date or datetime; a bare date means end-of-day UTC). Only past
    moments engage the vintage path — an as-of of "today, end of day" is
    equivalent to live and falls back to the live path.
    """
    as_of = as_of or os.environ.get(ASOF_ENV_VAR)
    if not as_of:
        return None
    as_of = str(as_of)
    dt = _parse_dt(f"{as_of}T23:59:59+00:00") if len(as_of) == 10 else _parse_dt(as_of)
    if dt is None:
        logger.warning("unparsable as-of %r — staying live", as_of)
        return None
    today_end = datetime.now(timezone.utc).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    return dt if dt < today_end else None


def _market_live_at(market: dict, asof_ts: int) -> bool:
    """Was this market listed and still unresolved at ``asof_ts`` (epoch seconds)?

    Deliberately ignores ``closed`` — that field reflects NOW, not the snapshot
    date. The Gamma timestamps are the only point-in-time signals available:
    listed before the snapshot (startDate/createdAt) and resolving after it
    (endDate). Requires ``clobTokenIds`` so vintage odds can be pinned via the
    CLOB price history.
    """
    start = _parse_dt(market.get("startDate") or market.get("createdAt"))
    if start and start.timestamp() > asof_ts:
        return False
    end = _parse_dt(market.get("endDate"))
    if end and end.timestamp() < asof_ts:
        return False
    return bool(_parse_json_list(market.get("clobTokenIds"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def _clob_price_at(token_id: str, asof_ts: int):
    """Point-in-time odds from CLOB price history (~hourly resolution).

    Returns ``(price_at_or_before_asof, one_week_pp_change_or_None)``; both
    are ``None`` when the market has no history before the snapshot (not yet
    listed, or no trades in the lookback window).
    """
    data = _clob_request(
        "prices-history",
        {
            "market": token_id,
            "startTs": asof_ts - 8 * 86400,
            "endTs": asof_ts + 3600,
            "fidelity": 60,
        },
    )
    rows = [
        r
        for r in (data.get("history") or [])
        if isinstance(r.get("t"), int) and isinstance(r.get("p"), (int, float))
    ]
    known = [r for r in rows if r["t"] <= asof_ts]
    if not known:
        return None, None
    last = max(known, key=lambda r: r["t"])
    week_old = [r for r in rows if r["t"] <= asof_ts - 7 * 86400]
    wk_pp = None
    if week_old:
        wk_pp = last["p"] - max(week_old, key=lambda r: r["t"])["p"]
    return float(last["p"]), wk_pp


def _vintage_report(topic: str, limit: int, candidates: list, as_of: datetime) -> str:
    """Render the as-of report: vintage odds, honest absences, no fake volume."""
    asof_ts = int(as_of.timestamp())
    asof_label = as_of.date().isoformat()
    header = (
        f'## Polymarket prediction markets: "{topic}" (point-in-time: as of {asof_label})\n'
        "Vintage odds from Polymarket's public CLOB price history — what the "
        "crowd priced AT that date, not today.\n\n"
    )

    if not candidates:
        return header + (
            f"No prediction markets matched '{topic}' as of {asof_label}. "
            "Coverage is concentrated in macro, political, geopolitical, and "
            "crypto events; the market itself may simply not have been listed yet."
        )

    lines, omitted = [], 0
    for m in candidates[:limit]:
        outcomes = _parse_json_list(m.get("outcomes"))
        tokens = _parse_json_list(m.get("clobTokenIds"))
        price, wk_pp = (None, None)
        if tokens:
            price, wk_pp = _clob_price_at(str(tokens[0]), asof_ts)
        if price is None:
            omitted += 1
            lines.append(
                f"- **{m.get('question')}** — no CLOB price history before "
                f"{asof_label} (market likely listed later)"
            )
            continue
        label = outcomes[0] if outcomes else "Yes"
        end_date = (m.get("endDate") or "")[:10]
        wk_str = f", 1-week {wk_pp * 100:+.1f}pp" if wk_pp is not None else ""
        lines.append(
            f"- **{m.get('question')}** — {label} {price:.0%} "
            f"(resolves {end_date}{wk_str})"
        )

    footer = (
        f"\nPoint-in-time odds as of {asof_label} (UTC) from Polymarket's "
        "public CLOB price history — NOT current values. Traded-volume figures "
        "are deliberately omitted: the API's volume is lifetime-to-date, not "
        "period-correct, and would misstate market depth at the snapshot date. "
        f"Markets without pre-{asof_label} price history are shown as gaps "
        f"({omitted} here), not dropped."
    )
    return header + "\n".join(lines) + "\n" + footer


def get_prediction_markets(topic: str, limit: int | None = None, as_of: str | datetime | None = None) -> str:
    """Return prediction-market probabilities for an event topic, live or vintage.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        as_of: Optional ISO date/datetime pinning odds to a point in the past
            using Polymarket's public CLOB price history. Past moments only —
            an as-of of today is treated as live. Also settable via the
            ``TA_PREDICTION_MARKETS_ASOF`` env var so whole backfill lanes can
            pin the date without changing call sites.

    Returns:
        A markdown report of the matching markets with implied probabilities,
        resolution dates, and recent (1-week) moves; in as-of mode the odds
        come from CLOB history (~hourly resolution), volumes are omitted as
        retrospective, and markets not yet listed appear as honest gaps.
    """
    if limit is None:
        limit = DEFAULT_LIMIT

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    as_of_dt = _resolve_as_of(as_of)
    if as_of_dt is not None:
        asof_ts = int(as_of_dt.timestamp())
        candidates = [
            m
            for event in data.get("events", [])
            for m in event.get("markets", [])
            if _market_live_at(m, asof_ts)
        ]
        candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)
        return _vintage_report(topic, limit, candidates, as_of_dt)

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not candidates:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        label = outcomes[0] if outcomes else "Yes"
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        wk_str = (
            f", 1-week {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{m.get('question')}** — {label} {prob:.0%} "
            f"(${volume:,.0f} volume, resolves {end_date}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"
