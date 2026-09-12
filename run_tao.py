
import warnings, logging
warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

from dotenv import load_dotenv
load_dotenv("/root/TradingAgents/.env")

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True          # survive interruptions, resume on retry
ta = TradingAgentsGraph(
    debug=True,
    config=config,
    selected_analysts=["market", "social", "news"],   # crypto pipeline: no fundamentals
)
state, signal = ta.propagate("TAO-USD", "2026-09-12", asset_type="crypto")
print("\n===== FINAL SIGNAL =====", signal)
print("\n===== PM DECISION (head) =====")
print(state["final_trade_decision"][:2500])
