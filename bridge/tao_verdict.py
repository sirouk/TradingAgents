"""TAO external-verdict bridge: TradingAgents -> tao-fleet.

Runs one TradingAgents crypto analysis and drops a verdict into the tao-fleet
workspace as external_verdict.json (+ .md), matching the fleet's files+DM
cadence contract. The file is ALWAYS written — on failure it carries
run_ok=false and rating=null so downstream gating can fail closed.

Contract (report-only, mirrors fleet rules):
  - rating is advisory; REVIEW / null = "no opinion today"
  - no authority over draft_authorization; prices are advisory context

Usage:
  python bridge/tao_verdict.py [--date YYYY-MM-DD] [--symbol TAO-USD]
      [--target-dir ~/tao-fleet]
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tao_verdict_bridge")

# Location-independent: resolves from the script's own path so the bridge runs
# in a container or under any user, with env overrides for systemd/docker units.
REPO = Path(os.environ.get("TAO_BRIDGE_REPO", Path(__file__).resolve().parents[1]))
LOGS = Path(
    os.environ.get(
        "TAO_BRIDGE_LOG_DIR",
        os.path.join(os.path.expanduser("~"), ".tradingagents", "bridge-logs"),
    )
)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    # Group-readable: the files+DM cadence lane consumes these drops as another
    # host user (the fleet workspace uses a setgid group). Owner stays rw.
    os.chmod(tmp, 0o640)
    os.rename(tmp, path)


def parse_pm(decision_md: str) -> dict:
    """Pull the structured bits out of the PM decision markdown."""
    def grab(pattern):
        m = re.search(pattern, decision_md, re.S)
        return m.group(1).strip() if m else None

    rating = grab(r"\*\*Rating\*\*\s*[:：]\s*([A-Za-z ]+)")
    stop = grab(r"\*\*Stop Loss\*\*\s*[:：]\s*([^\n]+)")
    sizing = grab(r"\*\*Position Sizing\*\*\s*[:：]\s*([^\n]+)")
    summary = grab(r"\*\*Executive Summary\*\*\s*[:：]?\s*(.+?)(?=\n\*\*|\Z)")
    if rating and rating.lower() == "review":
        rating = "REVIEW"
    return {"rating": rating, "stop_loss": stop, "position_sizing": sizing,
            "executive_summary": (summary or "").strip()[:2000] or None}


def latest_close(symbol: str) -> float | None:
    """Deterministic last close from the Binance vendor (advisory context)."""
    try:
        from tradingagents.dataflows.binance import resolve_binance_symbol, fetch_klines_df
        from datetime import datetime, timedelta
        end = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
        df = fetch_klines_df(resolve_binance_symbol(symbol), start, end)
        return float(df["Close"].iloc[-1]) if not df.empty else None
    except Exception as e:  # advisory field — never fail the bridge on it
        log.warning("latest_close advisory fetch failed: %s", e)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--symbol", default="TAO-USD")
    ap.add_argument("--job-id", default=None)
    ap.add_argument("--asset-type", default="crypto")
    ap.add_argument("--target-dir", default=os.environ.get(
        "TAO_BRIDGE_TARGET_DIR", str(Path.home() / "tao-fleet")))
    ap.add_argument("--max-minutes", type=int, default=55)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")

    target = Path(args.target_dir)
    verdict_path = target / "external_verdict.json"
    md_path = target / "external_verdict.md"

    verdict = {
        "job_id": args.job_id,
        "symbol": args.symbol,
        "analysis_date": args.date,
        "generated_at": None,
        "elapsed_seconds": None,
        "run_ok": False,
        "rating": None,
        "stop_loss": None,
        "position_sizing": None,
        "executive_summary": None,
        "reference_close": None,
        "llm_lane": os.path.splitext(os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL", ""))[0] or None,
        "full_report": None,
        "notes": None,
    }

    t0 = time.time()
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        config["checkpoint_enabled"] = True
        ta = TradingAgentsGraph(
            debug=True, config=config,
            selected_analysts=["market", "social", "news"],
        )
        state, signal = ta.propagate(args.symbol, args.date, asset_type=args.asset_type)

        decision_md = state.get("final_trade_decision", "")
        trader_plan = state.get("trader_investment_plan", "")
        pm = parse_pm(decision_md)
        trader = parse_pm(trader_plan)
        verdict.update(
            run_ok=True,
            rating=pm["rating"] or (signal if signal else None),
            # PM sometimes writes stop/sizing as prose; the trader's plan carries
            # the labeled fields — fall back to it rather than emit nulls.
            stop_loss=pm["stop_loss"] or trader["stop_loss"],
            position_sizing=pm["position_sizing"] or trader["position_sizing"],
            executive_summary=pm["executive_summary"],
            reference_close=latest_close(args.symbol),
            notes="signal=%s" % signal,
        )
        md = state.get("final_trade_decision", "")
        atomic_write(md_path, f"# External verdict — {args.symbol} · {args.date}\n\n" + md)
        verdict["full_report"] = str(md_path)
    except Exception as e:
        verdict["notes"] = f"FAILED: {type(e).__name__}: {e}"
        log.error("bridge run failed:\n%s", traceback.format_exc())
    finally:
        verdict["elapsed_seconds"] = round(time.time() - t0, 1)
        verdict["generated_at"] = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat()
        atomic_write(verdict_path, json.dumps(verdict, indent=2) + "\n")
        log.info("verdict written: ok=%s rating=%s -> %s",
                 verdict["run_ok"], verdict["rating"], verdict_path)
    return 0 if verdict["run_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
