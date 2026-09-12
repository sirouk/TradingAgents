"""Per-role LLM fallback chains (llm_fallbacks / TRADINGAGENTS_LLM_FALLBACKS).

Primaries (OAuth subscription lanes) throttle under agent swarms; fallbacks
retry the same call on another provider via LangChain with_fallbacks.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph, _parse_llm_fallbacks


def _bare_graph(config):
    g = object.__new__(TradingAgentsGraph)
    g.config = config
    return g


# --- _parse_llm_fallbacks ---------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", {}])
def test_parse_empty(value):
    assert _parse_llm_fallbacks(value) == {}


@pytest.mark.unit
def test_parse_json_string():
    fb = _parse_llm_fallbacks('{"deep": [{"model": "B", "api_key": "k"}]}')
    assert fb == {"deep": [{"model": "B", "api_key": "k"}]}


@pytest.mark.unit
def test_parse_rejects_bad_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_llm_fallbacks("{not json")


@pytest.mark.unit
def test_parse_rejects_unknown_role():
    with pytest.raises(ValueError, match="unknown"):
        _parse_llm_fallbacks({"dep": [{"model": "B"}]})


@pytest.mark.unit
def test_parse_rejects_entry_without_model():
    with pytest.raises(ValueError, match="'model'"):
        _parse_llm_fallbacks({"quick": [{"provider": "openai"}]})


# --- _attach_fallbacks -------------------------------------------------------

def _chain_of(primary):
    """Wrap a mock primary so with_fallbacks is observable."""
    wrapped = MagicMock(name="chain")
    primary.with_fallbacks.return_value = wrapped
    return primary, wrapped


@pytest.mark.unit
def test_no_fallbacks_returns_primary():
    g = _bare_graph({"llm_provider": "openai", "backend_url": None})
    primary, _ = _chain_of(MagicMock())
    assert g._attach_fallbacks(primary, "deep", {}, {}) is primary
    primary.with_fallbacks.assert_not_called()


@pytest.mark.unit
def test_fallback_entries_inherit_primary_provider_and_backend():
    g = _bare_graph({"llm_provider": "openai", "backend_url": "http://x"})
    with patch("tradingagents.graph.trading_graph.create_llm_client") as factory:
        factory.return_value.get_llm.return_value = MagicMock()
        primary, wrapped = _chain_of(MagicMock())
        out = g._attach_fallbacks(primary, "quick", {"quick": [{"model": "B"}]}, {"temperature": 0.1})
    factory.assert_called_once_with(
        provider="openai", model="B", base_url="http://x", temperature=0.1
    )
    primary.with_fallbacks.assert_called_once_with([factory.return_value.get_llm.return_value])
    assert out is wrapped


@pytest.mark.unit
def test_entry_overrides_provider_backend_and_api_key():
    g = _bare_graph({"llm_provider": "openai", "backend_url": None})
    with patch("tradingagents.graph.trading_graph.create_llm_client") as factory:
        factory.return_value.get_llm.return_value = MagicMock()
        primary, _ = _chain_of(MagicMock())
        g._attach_fallbacks(
            primary, "deep",
            {"deep": [{"model": "kimi", "provider": "openai_compatible",
                       "backend_url": "https://llm.chutes.ai/v1", "api_key": "cpk_x"}]},
            {},
        )
    factory.assert_called_once_with(
        provider="openai_compatible", model="kimi",
        base_url="https://llm.chutes.ai/v1", api_key="cpk_x",
    )


@pytest.mark.unit
def test_env_json_used_when_config_absent(monkeypatch=None):
    monkeypatch = monkeypatch or _MonkeyPatch()
    monkeypatch.setenv("TRADINGAGENTS_LLM_FALLBACKS", '{"deep": [{"model": "B"}]}')
    g = _bare_graph({"llm_provider": "openai", "backend_url": None, "llm_fallbacks": None})
    from tradingagents.graph.trading_graph import _parse_llm_fallbacks as parse
    value = g.config.get("llm_fallbacks") or os.environ.get("TRADINGAGENTS_LLM_FALLBACKS")
    assert parse(value) == {"deep": [{"model": "B"}]}


class _MonkeyPatch:
    def setenv(self, k, v):
        os.environ[k] = v


# --- end-to-end at __init__: chains attach to both roles ---------------------

@pytest.mark.unit
def test_init_attaches_chains_to_deep_and_quick():
    created = []

    def _mk_client(**kw):
        client = MagicMock(name=f"client_{len(created)}")
        created.append(client)
        return client

    config = {
        "llm_provider": "openai", "backend_url": None,
        "deep_think_llm": "A", "quick_think_llm": "C",
        "data_cache_dir": "/tmp/ta-fbs-cache", "results_dir": "/tmp/ta-fbs-res",
        "llm_max_retries": None, "temperature": None, "max_tokens": None,
        "google_thinking_level": None, "openai_reasoning_effort": None, "anthropic_effort": None,
        "max_debate_rounds": 1, "max_risk_discuss_rounds": 1, "max_recur_limit": 10,
        "llm_fallbacks": {"deep": [{"model": "B"}], "quick": [{"model": "D"}]},
    }
    with patch("tradingagents.graph.trading_graph.create_llm_client", side_effect=_mk_client) as factory, patch.object(
        TradingAgentsGraph, "_create_tool_nodes", return_value={}
    ), patch("tradingagents.graph.trading_graph.TradingMemoryLog"), patch(
        "tradingagents.graph.trading_graph.GraphSetup"
    ), patch("tradingagents.graph.trading_graph.ConditionalLogic"), patch(
        "tradingagents.graph.trading_graph.Propagator"
    ), patch("tradingagents.graph.trading_graph.Reflector"):
        g = TradingAgentsGraph(config=config)

    # 2 primaries (A deep, C quick) + 2 fallbacks (B deep, D quick), in order.
    assert factory.call_count == 4
    deep_primary, quick_primary = created[0], created[1]
    deep_fb, quick_fb = created[2], created[3]
    assert g.deep_thinking_llm is deep_primary.get_llm.return_value.with_fallbacks.return_value
    assert g.quick_thinking_llm is quick_primary.get_llm.return_value.with_fallbacks.return_value
    deep_primary.get_llm.return_value.with_fallbacks.assert_called_once_with(
        [deep_fb.get_llm.return_value]
    )
    quick_primary.get_llm.return_value.with_fallbacks.assert_called_once_with(
        [quick_fb.get_llm.return_value]
    )


if __name__ == "__main__":
    unittest.main()
