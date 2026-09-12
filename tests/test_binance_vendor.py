"""Unit tests for the Binance spot crypto vendor (dataflows/binance.py).

All network access is mocked: exchangeInfo + klines fake the Binance REST
shapes, so the suite runs offline.
"""

import copy
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import tradingagents.default_config as default_config
from tradingagents.dataflows import binance
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError

_FAKE_TRADABLE = {"TAOUSDT", "BTCUSDT", "BTCUSDC", "ETHUSDT", "SHIBUSDT", "SOLUSDT"}


def _fake_exchange_info():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "symbols": [
            {"symbol": s, "status": "TRADING", "isSpotTradingAllowed": True}
            for s in _FAKE_TRADABLE
        ]
        + [{"symbol": "OLDUSDT", "status": "BREAK", "isSpotTradingAllowed": True}]
    }
    return resp


def _kline(day_ms: int, close: float):
    """One Binance kline row: [open_time, O, H, L, C, V, close_time, ...]."""
    return [day_ms, str(close), str(close + 1), str(close - 1), str(close), "12.5", day_ms + 86399999]


def _fake_klines_rows(days_back=(0, 1, 2)):
    import datetime as _dt

    today = _dt.datetime.now(_dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [_kline(int((today - _dt.timedelta(days=d)).timestamp() * 1000), 200.0 + d) for d in reversed(days_back)]


@pytest.mark.unit
class BinanceSymbolResolutionTests(unittest.TestCase):
    def setUp(self):
        binance._reset_exchange_info_cache()

    def test_rejects_non_crypto_without_network(self):
        with patch("tradingagents.dataflows.binance.requests.get") as get, self.assertRaises(NoMarketDataError):
            binance.resolve_binance_symbol("AAPL")
        get.assert_not_called()

    def test_rejects_forex_shape_without_network(self):
        # EURUSD parses as ISO forex upstream, not as a crypto pair here.
        with patch("tradingagents.dataflows.binance.requests.get") as get, self.assertRaises(NoMarketDataError):
            binance.resolve_binance_symbol("EURUSD=X")
        get.assert_not_called()

    def test_usd_quote_prefers_usdt_book(self):
        with patch("tradingagents.dataflows.binance.requests.get", return_value=_fake_exchange_info()):
            self.assertEqual(binance.resolve_binance_symbol("TAO-USD"), "TAOUSDT")

    def test_compact_broker_form_maps_to_same_pair(self):
        with patch("tradingagents.dataflows.binance.requests.get", return_value=_fake_exchange_info()):
            self.assertEqual(binance.resolve_binance_symbol("taousdt"), "TAOUSDT")

    def test_explicit_usdt_quote_keeps_pair(self):
        with patch("tradingagents.dataflows.binance.requests.get", return_value=_fake_exchange_info()):
            self.assertEqual(binance.resolve_binance_symbol("TAO-USDT"), "TAOUSDT")

    def test_unlisted_crypto_raises_no_data(self):
        with patch("tradingagents.dataflows.binance.requests.get", return_value=_fake_exchange_info()), self.assertRaises(NoMarketDataError) as ctx:
            binance.resolve_binance_symbol("XYZCOIN-USD")
        self.assertIn("XYZCOIN", ctx.exception.detail)

    def test_exchange_info_cached_across_calls(self):
        with patch("tradingagents.dataflows.binance.requests.get", return_value=_fake_exchange_info()) as get:
            binance.resolve_binance_symbol("TAO-USD")
            binance.resolve_binance_symbol("BTC-USD")
        self.assertEqual(get.call_count, 1)


@pytest.mark.unit
class BinanceStockReportTests(unittest.TestCase):
    def setUp(self):
        binance._reset_exchange_info_cache()
        set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    def _patch_network(self):
        exchange = patch(
            "tradingagents.dataflows.binance._tradable_spot_symbols",
            return_value=_FAKE_TRADABLE,
        )
        return exchange

    def test_get_stock_report_shape_matches_yfinance(self):
        rows = _fake_klines_rows()
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        resp.json.return_value = rows
        with self._patch_network(), patch(
            "tradingagents.dataflows.binance.requests.get", return_value=resp
        ):
            out = binance.get_stock("TAO-USD", "2026-09-09", "2026-09-12")
        self.assertIn("TAOUSDT (from TAO-USD)", out)
        self.assertIn("Total records: 3", out)
        self.assertIn("Open,High,Low,Close,Volume", out)

    def test_get_stock_empty_raises_no_data(self):
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        resp.json.return_value = []
        with self._patch_network(), patch(
            "tradingagents.dataflows.binance.requests.get", return_value=resp
        ), self.assertRaises(NoMarketDataError):
            binance.get_stock("TAO-USD", "2026-09-09", "2026-09-12")

    def test_small_prices_survive_rounding(self):
        """SHIB-scale closes must not round to 0 after report formatting."""
        self.assertEqual(binance._round_price(0.00001234), 0.00001234)
        self.assertEqual(binance._round_price(234.567), 234.57)

    def test_indicator_window_report_shape(self):
        frame = pd.DataFrame(
            {
                "Date": pd.date_range("2026-09-01", periods=10, freq="D"),
                "Open": range(10),
                "High": range(10),
                "Low": range(10),
                "Close": [float(x) + 1.0 for x in range(10)],
                "Volume": [1.0] * 10,
            }
        )
        with patch(
            "tradingagents.dataflows.binance.load_binance_ohlcv", return_value=frame
        ), patch("tradingagents.dataflows.binance.resolve_binance_symbol", return_value="TAOUSDT"):
            out = binance.get_binance_indicators_window("TAO-USD", "rsi", "2026-09-10", 3)
        self.assertIn("## rsi values from 2026-09-07 to 2026-09-10:", out)
        self.assertIn("2026-09-10:", out)
        self.assertIn("RSI: Measures momentum", out)


@pytest.mark.unit
class BinanceRoutingTests(unittest.TestCase):
    def setUp(self):
        set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    def test_yfinance_no_data_falls_back_to_binance(self):
        from tradingagents.dataflows import interface

        with patch.dict(
            interface.VENDOR_METHODS["get_stock_data"],
            {"yfinance": MagicMock(side_effect=NoMarketDataError("TAO-USD", "TAO-USD", "no rows")),
             "binance": MagicMock(return_value="REPORT")},
        ):
            out = interface.route_to_vendor("get_stock_data", "TAO-USD", "2026-09-09", "2026-09-12")
        self.assertEqual(out, "REPORT")

    def test_equity_stays_on_yfinance(self):
        from tradingagents.dataflows import interface

        yf_impl = MagicMock(return_value="YF-REPORT")
        bn_impl = MagicMock(return_value="BN-REPORT")
        with patch.dict(
            interface.VENDOR_METHODS["get_stock_data"],
            {"yfinance": yf_impl, "binance": bn_impl},
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-09-09", "2026-09-12")
        self.assertEqual(out, "YF-REPORT")
        bn_impl.assert_not_called()

    def test_default_config_chains_include_binance(self):
        cfg = copy.deepcopy(default_config.DEFAULT_CONFIG)
        self.assertEqual(cfg["data_vendors"]["core_stock_apis"], "yfinance,binance")
        self.assertEqual(cfg["data_vendors"]["technical_indicators"], "yfinance,binance")


@pytest.mark.unit
class ValidatorFallbackTests(unittest.TestCase):
    """market_data_validator bypasses route_to_vendor (needs frames, not
    reports) — it must still fall through the same loader chain for crypto
    Yahoo does not cover; the TAO first-run crash tracked this gap."""

    def _frame(self):
        return pd.DataFrame(
            {
                "Date": pd.date_range("2026-09-01", periods=5, freq="D"),
                "Open": [1.0] * 5, "High": [1.0] * 5, "Low": [1.0] * 5,
                "Close": [1.0] * 5, "Volume": [1.0] * 5,
            }
        )

    def test_falls_back_to_binance_loader(self):
        from tradingagents.dataflows import market_data_validator as v

        with patch.object(v, "load_ohlcv", side_effect=NoMarketDataError("TAO-USD", "TAO-USD", "stale")), patch(
            "tradingagents.dataflows.binance.load_binance_ohlcv", return_value=self._frame()
        ) as bn:
            df, source = v._verified_rows("TAO-USD", "2026-09-05")
        self.assertEqual(source, "Binance spot")
        self.assertEqual(len(df), 5)
        bn.assert_called_once()

    def test_stays_on_yfinance_loader(self):
        from tradingagents.dataflows import market_data_validator as v

        with patch.object(v, "load_ohlcv", return_value=self._frame()), patch(
            "tradingagents.dataflows.binance.load_binance_ohlcv"
        ) as bn:
            df, source = v._verified_rows("AAPL", "2026-09-05")
        self.assertEqual(source, "yfinance")
        bn.assert_not_called()

    def test_snapshot_marks_data_source(self):
        from tradingagents.dataflows import market_data_validator as v

        with patch.object(v, "load_ohlcv", side_effect=NoMarketDataError("TAO-USD")), patch(
            "tradingagents.dataflows.binance.load_binance_ohlcv", return_value=self._frame()
        ):
            out = v.build_verified_market_snapshot("TAO-USD", "2026-09-05")
        self.assertIn("- Data source: Binance spot", out)


if __name__ == "__main__":
    unittest.main()
