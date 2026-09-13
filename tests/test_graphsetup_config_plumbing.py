"""Regression guard: GraphSetup carries config to the news analyst kill switch.

4fa7117 documented: wiring read ``self.config`` but GraphSetup never stored
one — discovered live. This test pins the plumbing AND the analyst behavior.
"""
import pytest

pytestmark = pytest.mark.unit


def test_graphsetup_stores_config():
    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.setup import GraphSetup

    gs = GraphSetup(None, None, {}, ConditionalLogic(), config={"enable_prediction_markets": False})
    assert gs.config == {"enable_prediction_markets": False}
    gs2 = GraphSetup(None, None, {}, ConditionalLogic())
    assert gs2.config == {}


def test_news_analyst_prompt_strips_prediction_markets(monkeypatch):
    from tradingagents.agents.analysts.news_analyst import create_news_analyst

    class _FakeLLM:
        def bind_tools(self, tools):
            self.bound = tools
            return self

        def __rrshift__(self, other):
            return self

        def invoke(self, messages):
            return type("R", (), {"content": "report", "tool_calls": []})()

    class _Prompt:
        def __init__(self):
            self.partials = {}

        @classmethod
        def from_messages(cls, msgs):
            return cls()

        def partial(self, **kw):
            self.partials.update(kw)
            return self

        def __or__(self, other):
            # prompt | llm -> the fake chain is just the fake LLM (bound tools
            # were captured by bind_tools before tilting)
            return other

    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.ChatPromptTemplate", _Prompt)
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_instrument_context_from_state", lambda s: "")
    monkeypatch.setattr("tradingagents.agents.analysts.news_analyst.get_language_instruction", lambda: "")

    for enabled, expect in ((True, "get_prediction_markets"), (False, None)):
        llm = _FakeLLM()
        node = create_news_analyst(llm, config={"enable_prediction_markets": enabled})
        node({"trade_date": "2026-01-01", "asset_type": "crypto",
              "messages": [], "news_report": ""})
        names = [t.name for t in llm.bound]
        has = "get_prediction_markets" in names
        assert has is enabled, (enabled, names)
