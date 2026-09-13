#!/usr/bin/env python3
"""Verdict API — authenticated HTTP trigger + retrieval for the verdict bridge.

A small worker pool runs jobs in parallel (VERDICT_API_WORKERS); interactive
/api/run jobs always claim slots before backfill jobs, so a long backfill sweep
never starves the daily/realtime lane. Every run — success or failure — leaves an
external_verdict.json (run_ok=false on failure, timeout synthesizes one), so
downstream gating can always fail closed.

Endpoints (all /api/* require `Authorization: Bearer <token>`):
  GET  /healthz                       liveness (no auth)
  POST /api/run                       body optional {"symbol","date","asset_type"};
                                      -> 202 {"job_id","state":"queued","position"}
                                      same symbol+date already in flight -> 409 + its job_id
  POST /api/backfill                  body {"symbols":[...], "start_date","end_date",
                                      "asset_type"="crypto"} -> 202 {"batch_id","jobs_created",...};
                                      one job per symbol x date cell (calendar days inclusive,
                                      depth capped by VERDICT_API_BACKFILL_MAX_DAYS);
                                      backfill jobs yield to interactive /api/run jobs
  GET  /api/backfill                  batch list with per-state counts
  GET  /api/backfill/{id}             batch spec + state counts
  GET  /api/backfill/{id}/verdicts.json           every finished cell, keyed symbol -> date
  GET  /api/backfill/{id}/verdicts/{symbol}.json  one symbol's cells of that batch
  GET  /api/jobs?limit=&symbol=&batch_id=   newest-first job list with optional filters
  GET  /api/jobs/{id}                 job record + verdict summary
  GET  /api/jobs/{id}/verdict.json    run artifacts
  GET  /api/jobs/{id}/verdict.md
  GET  /api/verdict/latest.json|.md   latest finished verdict (flat contract files)

Config via env (no secrets in argv):
  VERDICT_API_TOKEN_FILE  file holding the bearer token (mode 0600)  [or VERDICT_API_TOKEN]
  VERDICT_API_PORT        listen port, default 8877
  VERDICT_API_STORE       verdict store dir, default ~/.tradingagents/verdict-store
  VERDICT_API_DB          sqlite jobs db, default <store>/../verdict-api.db
  VERDICT_API_RETENTION_DAYS   history retention, default 90
  VERDICT_API_JOB_TIMEOUT      per-job seconds, default 3900 (65 min)
  VERDICT_API_WORKERS          parallel run slots, default 2 (interactive jobs always
                               claim slots before backfill jobs)
  VERDICT_API_BACKFILL_MAX_DAYS  max backfill depth behind today, default 180
  VERDICT_API_SYMBOL / VERDICT_API_ASSET_TYPE   optional run defaults; when unset,
                                      the bridge's own CLI defaults are used.

History: <store>/history/<job_id>/ artifacts, janitor sweeps hourly. Jobs left
running/queued across a restart are reaped to failed (+ fail verdict) at startup.
"""

import hmac
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LISTEN_PORT = int(os.environ.get("VERDICT_API_PORT", "8877"))
STORE = Path(os.path.expanduser(os.environ.get(
    "VERDICT_API_STORE", "~/.tradingagents/verdict-store"))).resolve()
HISTORY = STORE / "history"
DB = Path(os.environ.get("VERDICT_API_DB", str(STORE.parent / "verdict-api.db")))
BRIDGE = str(Path(__file__).resolve().with_name("tao_verdict.py"))
RETENTION_DAYS = int(os.environ.get("VERDICT_API_RETENTION_DAYS", "90"))
JOB_TIMEOUT = int(os.environ.get("VERDICT_API_JOB_TIMEOUT", "3900"))
WORKERS = int(os.environ.get("VERDICT_API_WORKERS", "2"))
BACKFILL_MAX_DAYS = int(os.environ.get("VERDICT_API_BACKFILL_MAX_DAYS", "180"))
DEFAULT_SYMBOL = os.environ.get("VERDICT_API_SYMBOL")
DEFAULT_ASSET_TYPE = os.environ.get("VERDICT_API_ASSET_TYPE")
MAX_QUEUE = int(os.environ.get("VERDICT_API_MAX_QUEUE", "32"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verdict-api")

_tok_path = os.environ.get("VERDICT_API_TOKEN_FILE")
TOKEN = (Path(_tok_path).read_text().strip() if _tok_path else os.environ.get("VERDICT_API_TOKEN", ""))
if not TOKEN:
    raise SystemExit("no API token available (VERDICT_API_TOKEN_FILE / VERDICT_API_TOKEN)")

db_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, symbol TEXT, analysis_date TEXT, asset_type TEXT,
            state TEXT, position INTEGER, created_at TEXT, started_at TEXT, finished_at TEXT,
            exit_code INTEGER, run_ok INTEGER, rating TEXT, verdict_path TEXT, error TEXT,
            kind TEXT NOT NULL DEFAULT 'interactive', batch_id TEXT)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
        for col, spec in (("kind", "kind TEXT NOT NULL DEFAULT 'interactive'"),
                          ("batch_id", "batch_id TEXT")):
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {spec}")
        c.execute("""CREATE TABLE IF NOT EXISTS batches(
            id TEXT PRIMARY KEY, symbols TEXT, start_date TEXT, end_date TEXT,
            asset_type TEXT, created_at TEXT, n_jobs INTEGER)""")


def db_exec(sql, args=()):
    with db_lock, db() as c:
        c.execute(sql, args)


def db_rows(sql, args=()):
    with db_lock, db() as c:
        return c.execute(sql, args).fetchall()


def job_public(r):
    return {"job_id": r["id"], "symbol": r["symbol"], "date": r["analysis_date"],
            "asset_type": r["asset_type"], "state": r["state"], "created_at": r["created_at"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
            "run_ok": (None if r["run_ok"] is None else bool(r["run_ok"])),
            "rating": r["rating"], "error": r["error"], "kind": r["kind"],
            "batch_id": r["batch_id"], "artifact_base": f"/api/jobs/{r['id']}"}


def job_dir(jid):
    return HISTORY / jid


def batch_counts(batch_id):
    c = {r["state"]: r["n"] for r in
         db_rows("SELECT state, COUNT(*) n FROM jobs WHERE batch_id=? GROUP BY state", (batch_id,))}
    c["total"] = sum(v for k, v in c.items() if k != "total")
    return c


def write_fail_verdict(jdir, jid, symbol, date, notes):
    jdir.mkdir(parents=True, exist_ok=True)
    verdict = {"job_id": jid, "symbol": symbol, "analysis_date": date, "generated_at": now_iso(),
               "elapsed_seconds": None, "run_ok": False, "rating": None, "stop_loss": None,
               "position_sizing": None, "executive_summary": None, "reference_close": None,
               "llm_lane": None, "full_report": None, "notes": notes}
    tmp = jdir / ".external_verdict.json.tmp"
    tmp.write_text(json.dumps(verdict, indent=2) + "\n")
    os.chmod(tmp, 0o640)
    tmp.rename(jdir / "external_verdict.json")


def publish_latest(jdir):
    """Copy finished artifacts up to the flat cadence-contract paths (0640, atomic)."""
    for name in ("external_verdict.json", "external_verdict.md"):
        src = jdir / name
        if src.exists():
            dst = STORE / name
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o640)
            tmp.rename(dst)


def reap_stale():
    """Jobs interrupted by a restart must not hang in running/queued forever."""
    for r in db_rows("SELECT * FROM jobs WHERE state IN ('running','queued')"):
        jd = job_dir(r["id"])
        if not (jd / "external_verdict.json").exists():
            write_fail_verdict(jd, r["id"], r["symbol"], r["analysis_date"],
                               "FAILED: api restart interrupted the run before a verdict was written")
        db_exec("UPDATE jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                (now_iso(), "reaped at api startup", r["id"]))
        publish_latest(jd)
        log.info("reaped stale job %s -> failed", r["id"])


def claim_next_job():
    """Atomically claim the highest-priority queued job (interactive before
    backfill, then oldest). Returns the job row as it read at claim time."""
    with db_lock, db() as c:
        row = c.execute("""SELECT * FROM jobs WHERE state='queued'
                       ORDER BY CASE kind WHEN 'interactive' THEN 0 ELSE 1 END,
                                created_at
                       LIMIT 1""").fetchone()
        if row is None:
            return None
        c.execute("""UPDATE jobs SET state='running', started_at=?
                     WHERE id=? AND state='queued'""", (now_iso(), row["id"]))
        return row


def worker(slot):
    while True:
        row = claim_next_job()
        if row is None:
            time.sleep(1.5)
            continue
        jid = row["id"]
        jdir = job_dir(jid)
        jdir.mkdir(parents=True, exist_ok=True)
        # Backfill isolation: batch-scoped memory log (causal via the point-in-time
        # trade-date cutoffs) so backfilled runs neither pollute nor inherit from
        # the realtime lane's memory.
        run_env = dict(os.environ)
        if row["kind"] == "backfill":
            bmem = STORE / "backfill-memory" / f"{row['batch_id'] or 'loose'}"
            bmem.mkdir(parents=True, exist_ok=True)
            run_env["TRADINGAGENTS_MEMORY_LOG_PATH"] = str(bmem / "trading_memory.md")
        cmd = ["python", BRIDGE, "--job-id", jid, "--date", row["analysis_date"], "--symbol", row["symbol"],
               "--asset-type", row["asset_type"], "--target-dir", str(jdir)]
        log.info("job %s starting: %s %s (slot %d, %s)", jid, row["symbol"], row["analysis_date"], slot, row["kind"])
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True, env=run_env)
            timed_out = False
            try:
                out, _ = proc.communicate(timeout=JOB_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                out, _ = proc.communicate()
            for line in (out or "").splitlines():
                log.info("[job %s] %s", jid, line)
            vpath = jdir / "external_verdict.json"
            run_ok = rating = None
            if not vpath.exists():
                write_fail_verdict(jdir, jid, row["symbol"], row["analysis_date"],
                                   "FAILED: runner timeout/kill before verdict write" if timed_out
                                   else "FAILED: bridge exited without writing a verdict")
            try:
                v = json.loads(vpath.read_text())
                run_ok, rating = bool(v.get("run_ok")), v.get("rating")
            except Exception:
                pass
            state = "finished" if proc.returncode == 0 and run_ok else "failed"
            db_exec("""UPDATE jobs SET state=?, finished_at=?, exit_code=?, run_ok=?, rating=?,
                       verdict_path=?, error=? WHERE id=?""",
                    (state, now_iso(), proc.returncode, None if run_ok is None else int(run_ok),
                     rating, str(vpath), None if state == "finished" else "see artifact notes", jid))
            publish_latest(jdir)
            log.info("job %s %s run_ok=%s rating=%s", jid, state, run_ok, rating)
        except Exception as e:
            log.exception("job %s runner error", jid)
            write_fail_verdict(jdir, jid, row["symbol"], row["analysis_date"],
                               f"FAILED: api runner {type(e).__name__}: {e}")
            db_exec("UPDATE jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                    (now_iso(), str(e), jid))


def janitor():
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
            for r in db_rows("SELECT id, created_at FROM jobs"):
                try:
                    old = datetime.fromisoformat(r["created_at"]) < cutoff
                except Exception:
                    old = False
                if old:
                    shutil.rmtree(job_dir(r["id"]), ignore_errors=True)
                    db_exec("DELETE FROM jobs WHERE id=?", (r["id"],))
                    log.info("janitor: purged job %s (> %dd)", r["id"], RETENTION_DAYS)
            stale_b = db_exec("DELETE FROM batches WHERE created_at < ?", (cutoff.isoformat(),))
        except Exception:
            log.exception("janitor sweep failed")
        time.sleep(3600)


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,24}$")
JOBID = re.compile(r"^[0-9]{8}-[0-9a-f]{12}$")
BATCHID = re.compile(r"^bf-[0-9]{8}-[0-9a-f]{8}$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_address[0], fmt % args)

    def _send(self, code, obj, ctype="application/json"):
        body = (obj if isinstance(obj, bytes) else json.dumps(obj, indent=2).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authed(self):
        a = self.headers.get("Authorization", "")
        return a.startswith("Bearer ") and hmac.compare_digest(a[7:].strip(), TOKEN)

    def _require_auth(self):
        if not self._authed():
            self._send(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "ts": now_iso()})
        if not self._require_auth():
            return
        path, _, qs = self.path.partition("?")
        if path in ("/api/verdict/latest", "/api/verdict/latest.json"):
            return self._artifact(STORE / "external_verdict.json", "application/json")
        if path == "/api/verdict/latest.md":
            return self._artifact(STORE / "external_verdict.md", "text/markdown")
        if path == "/api/jobs":
            lim = 50
            m = re.search(r"(?:^|&)limit=(\d+)", qs)
            if m:
                lim = max(1, min(2000, int(m.group(1))))
            where, args = ["1=1"], []
            for key in ("symbol", "batch_id"):
                m = re.search(rf"(?:^|&){key}=([^&]+)", qs)
                if m:
                    where.append(f"{key}=?")
                    args.append(m.group(1))
            rows = db_rows("SELECT * FROM jobs WHERE " + " AND ".join(where) +
                           " ORDER BY created_at DESC LIMIT ?", tuple(args + [lim]))
            return self._send(200, {"jobs": [job_public(r) for r in rows]})
        if path == "/api/backfill":
            batches = []
            for b in db_rows("SELECT * FROM batches ORDER BY created_at DESC LIMIT 50"):
                batches.append({"batch_id": b["id"], "symbols": json.loads(b["symbols"]),
                                "start_date": b["start_date"], "end_date": b["end_date"],
                                "asset_type": b["asset_type"], "created_at": b["created_at"],
                                **batch_counts(b["id"])})
            return self._send(200, {"batches": batches})
        m = re.match(r"^/api/backfill/([^/]+)(/.*)?$", path)
        if m and BATCHID.match(m.group(1)):
            bid, sub = m.group(1), m.group(2) or ""
            brow = db_rows("SELECT * FROM batches WHERE id=?", (bid,))
            if not brow:
                return self._send(404, {"error": "unknown batch_id"})
            b = brow[0]
            if sub == "":
                return self._send(200, {"batch_id": bid, "symbols": json.loads(b["symbols"]),
                                        "start_date": b["start_date"], "end_date": b["end_date"],
                                        "asset_type": b["asset_type"], "created_at": b["created_at"],
                                        **batch_counts(bid)})
            if sub == "/verdicts.json":
                return self._bundle(bid, None)
            m2 = re.match(r"^/verdicts/([^/]+)\.json$", sub)
            if m2:
                return self._bundle(bid, m2.group(1).upper())
        m = re.match(r"^/api/jobs/([^/]+)(/.*)?$", path)
        if m and JOBID.match(m.group(1)):
            jid, sub = m.group(1), m.group(2) or ""
            rows = db_rows("SELECT * FROM jobs WHERE id=?", (jid,))
            if not rows:
                return self._send(404, {"error": "unknown job_id"})
            if sub == "":
                return self._send(200, job_public(rows[0]))
            if sub == "/verdict.json":
                return self._artifact(job_dir(jid) / "external_verdict.json", "application/json")
            if sub == "/verdict.md":
                return self._artifact(job_dir(jid) / "external_verdict.md", "text/markdown")
        return self._send(404, {"error": "no such route"})

    def _read_body(self, limit=65536):
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            n = 0
        body = self.rfile.read(min(n, limit)) if n else b"{}"
        try:
            req = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None
        return req if isinstance(req, dict) else None

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        if path not in ("/api/run", "/api/backfill"):
            return self._send(404, {"error": "no such route"})
        if not self._require_auth():
            return
        req = self._read_body()
        if req is None:
            return self._send(400, {"error": "invalid JSON body"})
        if path == "/api/run":
            return self._post_run(req)
        return self._post_backfill(req)

    def _post_run(self, req):
        symbol = str(req.get("symbol", DEFAULT_SYMBOL or "")).upper()
        date = str(req.get("date", time.strftime("%Y-%m-%d")))
        asset_type = str(req.get("asset_type", DEFAULT_ASSET_TYPE or "")).lower()
        if symbol and not SYMBOL.match(symbol):
            return self._send(400, {"error": "bad symbol"})
        if not ISO_DATE.match(date) or (asset_type and asset_type not in ("crypto", "equity")):
            return self._send(400, {"error": "bad date/asset_type"})
        queued = db_rows("SELECT COUNT(*) n FROM jobs WHERE state='queued' AND kind='interactive'")[0]["n"]
        if queued >= MAX_QUEUE:
            return self._send(429, {"error": "queue full"})
        dup = db_rows("""SELECT id FROM jobs WHERE symbol=? AND analysis_date=?
                         AND state IN ('queued','running')""", (symbol, date))
        if dup:
            return self._send(409, {"error": "duplicate job already in flight", "job_id": dup[0]["id"]})
        jid = f"{date.replace('-', '')}-{uuid.uuid4().hex[:12]}"
        db_exec("""INSERT INTO jobs(id,symbol,analysis_date,asset_type,state,position,created_at,kind)
                   VALUES(?,?,?,?,'queued',?,?,'interactive')""",
                (jid, symbol, date, asset_type, queued + 1, now_iso()))
        log.info("enqueued job %s (%s %s) pos=%d", jid, symbol or "<bridge default>", date, queued + 1)
        return self._send(202, {"job_id": jid, "state": "queued", "position": queued + 1})

    def _post_backfill(self, req):
        symbols = req.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [str(x).upper() for x in symbols]
        start = str(req.get("start_date", ""))
        end = str(req.get("end_date", ""))
        asset_type = str(req.get("asset_type", DEFAULT_ASSET_TYPE or "crypto")).lower()
        errs = []
        if not symbols or len(symbols) > 50:
            errs.append("symbols must be a list of 1..50 tickers")
        bad = [x for x in symbols if not SYMBOL.match(x)]
        if bad:
            errs.append(f"bad symbols: {bad[:5]}")
        if not (ISO_DATE.match(start) and ISO_DATE.match(end)):
            errs.append("start_date/end_date must be YYYY-MM-DD")
        if asset_type not in ("crypto", "equity"):
            errs.append("asset_type must be crypto or equity")
        if errs:
            return self._send(400, {"error": "; ".join(errs)})
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        if end_d < start_d:
            return self._send(400, {"error": "end_date before start_date"})
        if end_d > today:
            return self._send(400, {"error": "end_date in the future"})
        if (today - start_d).days > BACKFILL_MAX_DAYS:
            return self._send(400, {"error": f"start_date beyond backfill window ({BACKFILL_MAX_DAYS} days)"})
        dates = [(start_d + timedelta(days=i)).isoformat()
                 for i in range((end_d - start_d).days + 1)]
        batch_id = time.strftime("bf-%Y%m%d-", time.gmtime()) + uuid.uuid4().hex[:8]
        created = skipped = 0
        for sym in symbols:
            for d in dates:
                dup = db_rows("""SELECT id FROM jobs WHERE symbol=? AND analysis_date=?
                                 AND state IN ('queued','running')""", (sym, d))
                if dup:
                    skipped += 1
                    continue
                jid = f"{d.replace('-', '')}-{uuid.uuid4().hex[:12]}"
                db_exec("""INSERT INTO jobs(id,symbol,analysis_date,asset_type,state,position,created_at,kind,batch_id)
                           VALUES(?,?,?,?,'queued',0,?,'backfill',?)""",
                        (jid, sym, d, asset_type, now_iso(), batch_id))
                created += 1
        db_exec("""INSERT INTO batches(id,symbols,start_date,end_date,asset_type,created_at,n_jobs)
                   VALUES(?,?,?,?,?,?,?)""",
                (batch_id, json.dumps(sorted(symbols)), start, end, asset_type, now_iso(), created))
        log.info("backfill batch %s: %d jobs enqueued (%d skipped in-flight), %s %s..%s x%d symbols",
                 batch_id, created, skipped, asset_type, start, end, len(symbols))
        return self._send(202, {"batch_id": batch_id, "jobs_created": created,
                                "cells_skipped_in_flight": skipped, "days": len(dates),
                                "symbols": symbols, "status_url": f"/api/backfill/{batch_id}"})

    def _bundle(self, batch_id, symbol):
        if symbol:
            rows = db_rows("""SELECT * FROM jobs WHERE batch_id=? AND symbol=?
                              ORDER BY analysis_date""", (batch_id, symbol))
        else:
            rows = db_rows("SELECT * FROM jobs WHERE batch_id=? ORDER BY symbol, analysis_date", (batch_id,))
        if not rows:
            return self._send(404, {"error": "no jobs" + (f" for symbol {symbol}" if symbol else "")})
        cells = {}
        for r in rows:
            vpath = job_dir(r["id"]) / "external_verdict.json"
            cell = None
            if vpath.exists():
                try:
                    cell = json.loads(vpath.read_text())
                except Exception:
                    cell = {"job_id": r["id"], "notes": "artifact unreadable"}
            if cell is None:
                cell = {"job_id": r["id"], "symbol": r["symbol"], "analysis_date": r["analysis_date"],
                        "state": r["state"], "run_ok": None, "rating": None,
                        "notes": ("run not finished yet" if r["state"] in ("queued", "running")
                                  else (r["error"] or "no artifact"))}
            cells.setdefault(r["symbol"], {})[r["analysis_date"]] = cell
        out = {"batch_id": batch_id, "generated_at": now_iso(),
               **batch_counts(batch_id), "verdicts": cells}
        body = json.dumps(out, indent=2).encode()
        if len(body) > 32 * 1024 * 1024:
            return self._send(413, {"error": "bundle too large; fetch per-symbol"})
        return self._send(200, body)

    def _artifact(self, path: Path, ctype):
        if not path.exists():
            return self._send(404, {"error": "artifact not yet available"})
        data = path.read_bytes()
        if len(data) > 8 * 1024 * 1024:
            return self._send(413, {"error": "artifact too large"})
        return self._send(200, data, ctype)


def main():
    db_init()
    HISTORY.mkdir(parents=True, exist_ok=True)
    reap_stale()
    for slot in range(WORKERS):
        threading.Thread(target=worker, args=(slot,), daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info("verdict-api listening on :%d store=%s db=%s workers=%d",
             LISTEN_PORT, STORE, DB, WORKERS)
    srv.serve_forever()


if __name__ == "__main__":
    main()
