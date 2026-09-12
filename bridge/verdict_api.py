#!/usr/bin/env python3
"""Verdict API — authenticated HTTP trigger + retrieval for the verdict bridge.

One job runs at a time (LLM lanes are rate-limited; matches the flock discipline
of the standalone systemd service). Every run — success or failure — leaves an
external_verdict.json (run_ok=false on failure, timeout synthesizes one), so
downstream gating can always fail closed.

Endpoints (all /api/* require `Authorization: Bearer <token>`):
  GET  /healthz                       liveness (no auth)
  POST /api/run                       body optional {"symbol","date","asset_type"};
                                      -> 202 {"job_id","state":"queued","position"}
  GET  /api/jobs?limit=               newest-first job list
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
  VERDICT_API_SYMBOL / VERDICT_API_ASSET_TYPE   optional run defaults; when unset,
                                      the bridge's own CLI defaults are used.

History: <store>/history/<job_id>/ artifacts, janitor sweeps hourly. Jobs left
running/queued across a restart are reaped to failed (+ fail verdict) at startup.
"""

import hmac
import json
import logging
import os
import queue
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
DEFAULT_SYMBOL = os.environ.get("VERDICT_API_SYMBOL")
DEFAULT_ASSET_TYPE = os.environ.get("VERDICT_API_ASSET_TYPE")
MAX_QUEUE = 32

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("verdict-api")

_tok_path = os.environ.get("VERDICT_API_TOKEN_FILE")
TOKEN = (Path(_tok_path).read_text().strip() if _tok_path else os.environ.get("VERDICT_API_TOKEN", ""))
if not TOKEN:
    raise SystemExit("no API token available (VERDICT_API_TOKEN_FILE / VERDICT_API_TOKEN)")

_jobs = queue.Queue()
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
            exit_code INTEGER, run_ok INTEGER, rating TEXT, verdict_path TEXT, error TEXT)""")


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
            "rating": r["rating"], "error": r["error"],
            "artifact_base": f"/api/jobs/{r['id']}"}


def job_dir(jid):
    return HISTORY / jid


def write_fail_verdict(jdir, symbol, date, notes):
    jdir.mkdir(parents=True, exist_ok=True)
    verdict = {"symbol": symbol, "analysis_date": date, "generated_at": now_iso(),
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
            write_fail_verdict(jd, r["symbol"], r["analysis_date"],
                               "FAILED: api restart interrupted the run before a verdict was written")
        db_exec("UPDATE jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                (now_iso(), "reaped at api startup", r["id"]))
        publish_latest(jd)
        log.info("reaped stale job %s -> failed", r["id"])


def worker():
    while True:
        jid = _jobs.get()
        if jid is None:
            return
        row = db_rows("SELECT * FROM jobs WHERE id=?", (jid,))[0]
        jdir = job_dir(jid)
        jdir.mkdir(parents=True, exist_ok=True)
        db_exec("UPDATE jobs SET state='running', started_at=? WHERE id=?", (now_iso(), jid))
        cmd = ["python", BRIDGE, "--date", row["analysis_date"], "--symbol", row["symbol"],
               "--asset-type", row["asset_type"], "--target-dir", str(jdir)]
        log.info("job %s starting: %s %s", jid, row["symbol"], row["analysis_date"])
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, start_new_session=True)
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
                write_fail_verdict(jdir, row["symbol"], row["analysis_date"],
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
            write_fail_verdict(jdir, row["symbol"], row["analysis_date"],
                               f"FAILED: api runner {type(e).__name__}: {e}")
            db_exec("UPDATE jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                    (now_iso(), str(e), jid))
        finally:
            _jobs.task_done()


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
        except Exception:
            log.exception("janitor sweep failed")
        time.sleep(3600)


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL = re.compile(r"^[A-Za-z0-9.\-]{1,24}$")
JOBID = re.compile(r"^[0-9]{8}-[0-9a-f]{12}$")


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
                lim = max(1, min(200, int(m.group(1))))
            rows = db_rows("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (lim,))
            return self._send(200, {"jobs": [job_public(r) for r in rows]})
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

    def do_POST(self):
        if self.path != "/api/run":
            return self._send(404, {"error": "no such route"})
        if not self._require_auth():
            return
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            n = 0
        body = self.rfile.read(min(n, 8192)) if n else b"{}"
        try:
            req = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "invalid JSON body"})
        symbol = str(req.get("symbol", DEFAULT_SYMBOL or "")).upper()
        date = str(req.get("date", time.strftime("%Y-%m-%d")))
        asset_type = str(req.get("asset_type", DEFAULT_ASSET_TYPE or "")).lower()
        if symbol and not SYMBOL.match(symbol):
            return self._send(400, {"error": "bad symbol"})
        if not ISO_DATE.match(date) or (asset_type and asset_type not in ("crypto", "equity")):
            return self._send(400, {"error": "bad date/asset_type"})
        queued = db_rows("SELECT COUNT(*) n FROM jobs WHERE state='queued'")[0]["n"]
        if queued >= MAX_QUEUE:
            return self._send(429, {"error": "queue full"})
        dup = db_rows("""SELECT id FROM jobs WHERE symbol=? AND analysis_date=?
                         AND state IN ('queued','running')""", (symbol, date))
        if dup:
            return self._send(409, {"error": "duplicate job already in flight", "job_id": dup[0]["id"]})
        jid = f"{date.replace('-', '')}-{uuid.uuid4().hex[:12]}"
        db_exec("""INSERT INTO jobs(id,symbol,analysis_date,asset_type,state,position,created_at)
                   VALUES(?,?,?,?,'queued',?,?)""", (jid, symbol, date, asset_type, queued + 1, now_iso()))
        _jobs.put(jid)
        log.info("enqueued job %s (%s %s) pos=%d", jid, symbol or "<bridge default>", date, queued + 1)
        return self._send(202, {"job_id": jid, "state": "queued", "position": queued + 1})

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
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=janitor, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    log.info("verdict-api listening on :%d store=%s db=%s", LISTEN_PORT, STORE, DB)
    try:
        srv.serve_forever()
    finally:
        _jobs.put(None)


if __name__ == "__main__":
    main()
