"""Binance spot OHLCV vendor for crypto symbols Yahoo Finance does not cover.

Yahoo lists only the largest crypto pairs (BTC-USD, ETH-USD, ...; #982). Longer-
tail coins — TAO, many USDT-quoted alts — return empty frames there and the
router would emit the NO_DATA sentinel even though liquid markets exist.

This vendor serves daily OHLCV for spot pairs from Binance's public REST API
(``/api/v3/klines``, ``/api/v3/exchangeInfo``) — no key, no auth. It is wired
into the vendor chain after ``yfinance``, so it only answers when Yahoo has no
rows for a crypto-shaped symbol; equities never reach it (a symbol that does
not look like a crypto pair is rejected before any network call).

Only ``core_stock_apis`` and ``technical_indicators`` are implemented: Binance
has no fundamentals, news, or insider data. Those tools keep their existing
vendors, and crypto analyses already drop the fundamentals analyst (#567).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

import pandas as pd
import requests

from .config import get_config
from .errors import NoMarketDataError
from .stockstats_utils import (
    _assert_ohlcv_not_stale,
    _clean_dataframe,
    _fill_price_gaps,
    _needs_same_day_refresh,
)
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"
REQUEST_TIMEOUT = 30

# Reuse the indicator-description map so both OHLCV vendors emit reference text
# the Market Analyst already knows how to read.
from .y_finance import INDICATOR_DESCRIPTIONS  # noqa: E402

# Quote assets Binance spot pairs trade against, in preference order. A user
# asking for ``FOO-USD`` almost always wants the FOO/USDT book (deepest
# liquidity); plain ``USD`` pairs are rare on Binance spot.
_QUOTE_PREFERENCE = ("USDT", "USDC", "FDUSD", "USD")

# 24h in-memory cache of tradable spot symbols; plenty stable day to day.
_EXCHANGE_INFO_TTL_SECONDS = 24 * 3600
_exchange_info_cache: tuple[float, frozenset[str]] | None = None


def _tradable_spot_symbols() -> frozenset[str]:
    """The set of symbols tradable on Binance spot right now (cached, 24h TTL)."""
    global _exchange_info_cache
    now = time.time()
    if _exchange_info_cache and now - _exchange_info_cache[0] < _EXCHANGE_INFO_TTL_SECONDS:
        return _exchange_info_cache[1]
    resp = requests.get(
        f"{BINANCE_BASE}/api/v3/exchangeInfo",
        params={"showPermissionSets": "false"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    symbols = frozenset(
        s["symbol"]
        for s in resp.json().get("symbols", [])
        if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
    )
    _exchange_info_cache = (now, symbols)
    return symbols


def _reset_exchange_info_cache() -> None:
    """Test hook: drop the cached exchange-info snapshot."""
    global _exchange_info_cache
    _exchange_info_cache = None


def resolve_binance_symbol(symbol: str) -> str:
    """Map a crypto-shaped user/Yahoo symbol to a live Binance spot symbol.

    Accepts dashed and compact forms with a recognized quote asset
    (``TAO-USD``, ``TAO-USDT``, ``TAOUSDT``, ``BTCUSDC``). Anything else —
    equities, indices, forex — is rejected *without a network call* (this
    vendor sits behind yfinance in the chain and must not shadow it), as is a
    crypto-shaped symbol Binance does not list.

    Quote preference when the user writes ``-USD``: ``USDT``, then ``USDC``,
    ``FDUSD``, plain ``USD`` — the deepest book wins the mapping.
    """
    raw = str(symbol).strip().upper().rstrip("+").replace("/", "-")
    compact = raw.replace("-", "")

    base = quote = None
    # Split off a trailing quote asset (longest first so USDT matches before USD).
    for q in sorted(_QUOTE_PREFERENCE, key=len, reverse=True) + ["BTC", "ETH", "BNB"]:
        if compact.endswith(q) and len(compact) > len(q):
            base, quote = compact[: -len(q)], q
            break
    if base is None:
        raise NoMarketDataError(
            symbol, symbol, "not a crypto pair symbol (no recognized quote asset) — not a Binance query"
        )

    try:
        tradable = _tradable_spot_symbols()
    except Exception as e:
        # Exchange-listing discovery failed; do not guess pair names — report
        # the vendor as broken (router falls through/errors loudly, #989).
        raise RuntimeError(f"Binance exchangeInfo lookup failed: {e}") from e

    candidates = [base + quote] if quote != "USD" else []
    candidates += [base + q for q in _QUOTE_PREFERENCE]
    for candidate in dict.fromkeys(candidates):  # ordered de-dup
        if candidate in tradable:
            if candidate != raw:
                logger.info("Binance: resolved %s -> %s", symbol, candidate)
            return candidate

    raise NoMarketDataError(
        symbol, base, f"no tradable Binance spot pair for base {base!r} (tried {', '.join(dict.fromkeys(candidates))})"
    )


def fetch_klines_df(
    binance_symbol: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """Daily klines for ``binance_symbol`` over [start_date, end_date] inclusive.

    Returns a yfinance-shaped frame: ``Date`` column (naive UTC date) plus
    numeric ``Open/High/Low/Close/Volume``. Empty range raises
    NoMarketDataError, matching the yfinance vendor's contract.
    """
    start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    # Binance klines filter on the bar OPEN time; a daily bar opens at 00:00 UTC,
    # so the last wanted open is end_date 00:00 itself.
    end_ms = int((datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000) - 1

    rows = []
    cursor = start_ms
    while True:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={
                "symbol": binance_symbol,
                "interval": "1d",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429 or resp.status_code == 418:
            from .errors import VendorRateLimitError
            raise VendorRateLimitError(f"Binance rate-limited ({resp.status_code})")
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        cursor = last_open + 1
        if len(batch) < 1000:
            break

    if not rows:
        raise NoMarketDataError(
            binance_symbol, binance_symbol, f"no klines between {start_date} and {end_date}"
        )

    data = pd.DataFrame(
        {
            "Date": pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True).tz_convert(None),
            "Open": [float(r[1]) for r in rows],
            "High": [float(r[2]) for r in rows],
            "Low": [float(r[3]) for r in rows],
            "Close": [float(r[4]) for r in rows],
            "Volume": [float(r[5]) for r in rows],
        }
    )
    return data.drop_duplicates(subset="Date").sort_values("Date").reset_index(drop=True)


def _round_price(v: float) -> float:
    """Round like the yfinance vendor at equity scale, but keep small-cap crypto
    prices (SHIB & friends) from rounding to zero."""
    return round(v, 2) if abs(v) >= 1 else float(f"{v:.8g}")


def get_stock(
    symbol: Annotated[str, "ticker symbol (crypto pair in any supported form)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):
    """Drop-in OHLCV vendor for crypto — same report shape as yfinance."""
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    canonical = resolve_binance_symbol(symbol)
    data = fetch_klines_df(canonical, start_date, end_date)

    _assert_ohlcv_not_stale(data, end_date, symbol, canonical)

    for col in ("Open", "High", "Low", "Close"):
        data[col] = data[col].map(_round_price)

    csv_string = data.set_index("Date").to_csv()

    label = canonical if canonical == str(symbol).upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date} [Binance spot, daily]\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + csv_string


def load_binance_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Mirror of ``stockstats_utils.load_ohlcv`` against Binance klines.

    Cached per symbol (5y window to today, same cache dir, ``-Binance-`` tag);
    rows after ``curr_date`` are filtered so backtests never see future prices.
    """
    canonical = resolve_binance_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date).normalize()

    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = today_date.strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-Binance-data-{start_str}-{end_str}.csv",
    )

    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if (
            not cached.empty
            and "Close" in cached.columns
            and not _needs_same_day_refresh(data_file, curr_date_dt, today_date)
        ):
            data = cached

    if data is None:
        downloaded = fetch_klines_df(canonical, start_str, end_str)
        if downloaded.empty or "Close" not in downloaded.columns:
            raise NoMarketDataError(canonical, canonical, "Binance returned no rows")
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting.
    data = data[data["Date"] <= curr_date_dt]
    data = _fill_price_gaps(data)

    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)
    return data


def _get_binance_stock_stats(symbol: str, indicator: str, curr_date: str):
    """Single-day indicator value, binance loader equivalent of StockstatsUtils."""
    from stockstats import wrap

    data = load_binance_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    df[indicator]
    matching = df[df["Date"].str.startswith(pd.to_datetime(curr_date).strftime("%Y-%m-%d"))]
    if not matching.empty:
        return matching[indicator].values[0]
    return "N/A: Not a trading day (weekend or holiday)"


def get_binance_indicators_window(
    symbol: Annotated[str, "ticker symbol (crypto pair in any supported form)"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Indicator series over the look-back window — same report shape as the
    yfinance vendor so the Market Analyst reads one format."""
    from stockstats import wrap

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - timedelta(days=look_back_days)

    try:
        data = load_binance_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        df[indicator]
        indicator_data = {
            row["Date"]: ("N/A" if pd.isna(row[indicator]) else str(row[indicator]))
            for _, row in df.iterrows()
        }

        date_values = []
        cursor = curr_date_dt
        while cursor >= before:
            date_str = cursor.strftime("%Y-%m-%d")
            date_values.append((date_str, indicator_data.get(date_str, "N/A: Not a trading day (weekend or holiday)")))
            cursor = cursor - timedelta(days=1)

        ind_string = "".join(f"{d}: {v}\n" for d, v in date_values)

    except NoMarketDataError:
        raise
    except Exception as e:
        logger.warning("Bulk binance stockstats fetch failed, falling back per-day: %s", e)
        ind_string = ""
        cursor = curr_date_dt
        while cursor >= before:
            day = cursor.strftime("%Y-%m-%d")
            try:
                value = _get_binance_stock_stats(symbol, indicator, day)
            except Exception as day_err:  # single-day compute must not kill the window
                logger.warning("Binance indicator %s failed on %s: %s", indicator, day, day_err)
                value = ""
            ind_string += f"{day}: {value}\n"
            cursor = cursor - timedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + ind_string
        + "\n\n"
        + INDICATOR_DESCRIPTIONS.get(indicator, "No description available.")
    )
