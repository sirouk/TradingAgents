"""Unit tests for the verdict API batch/backfill/claim surface.

The full HTTP server runs in-process on an ephemeral port with the worker and
janitor threads stubbed out, so jobs stay queued/finished exactly as the tests
stage them — no LLM runs, no network beyond loopback, no externals beyond
stdlib.
"""
import importlib.util
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.unit

BRIDGE_API = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                          "bridge", "verdict_api.py")
TOKEN = "test-token-for-verdict-api"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ApiServer:
    def __init__(self):
        self.module = None
        self.thread = None
        self.base = None

    def start(self, tmp_path, monkeypatch):
        store = tmp_path / "store"
        db = tmp_path / "verdict-api.db"
        monkeypatch.setenv("VERDICT_API_TOKEN", TOKEN)
        monkeypatch.setenv("VERDICT_API_STORE", str(store))
        monkeypatch.setenv("VERDICT_API_DB", str(db))
        monkeypatch.setenv("VERDICT_API_PORT", str(_free_port()))
        monkeypatch.setenv("VERDICT_API_WORKERS", "2")
        spec = importlib.util.spec_from_file_location("verdict_api_under_test", BRIDGE_API)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # keep workers + janitor out of the way; the tests stage job states by hand
        monkeypatch.setattr(module, "worker", lambda slot: None)
        monkeypatch.setattr(module, "janitor", lambda: None)
        monkeypatch.setattr(module, "reap_stale", lambda: None)
        self.module = module
        self.thread = threading.Thread(target=module.main, daemon=True)
        self.thread.start()
        host = module.LISTEN_PORT
        self.base = f"http://127.0.0.1:{host}"
        self._wait_ready()
        return self

    def _wait_ready(self):
        for _ in range(100):
            try:
                self.get("/healthz", auth=False)
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("api under test never came up")

    def _req(self, method, path, payload=None, auth=True, raw=False):
        url = self.base + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                body = r.read()
                return r.status, (body.decode() if raw else json.loads(body))
        except urllib.error.HTTPError as e:
            ebody = e.read() or b"{}"
            if raw:
                return e.code, ebody.decode()
            try:
                return e.code, json.loads(ebody)
            except json.JSONDecodeError:
                return e.code, {}

    def get_raw(self, path, auth=True):
        return self._req("GET", path, None, auth, raw=True)

    def post(self, path, payload=None, auth=True):
        return self._req("POST", path, payload, auth)

    def get(self, path, auth=True):
        return self._req("GET", path, None, auth)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    return ApiServer().start(tmp_path, monkeypatch)


def _today(module):
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _past(days):
    import datetime as dt
    return (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days)).isoformat()


# ---- auth & basics ----------------------------------------------------------

def test_auth_required(api):
    code, body = api.get("/api/jobs", auth=False)
    assert code == 401


def test_healthz_no_auth(api):
    code, body = api.get("/healthz", auth=False)
    assert code == 200 and body["ok"] is True


# ---- /api/backfill validation ----------------------------------------------

def test_backfill_rejects_bad_inputs(api):
    code, body = api.post("/api/backfill", {"symbols": ["BTC-USD"], "end_date": "2025-05-01"})
    assert code == 400 and "start_date" in body["error"]
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD"], "start_date": "2025-05-10", "end_date": "2025-05-01"})
    assert code == 400 and "end_date before start_date" in body["error"]
    code, body = api.post("/api/backfill",
                          {"symbols": ["not a symbol!"], "start_date": _past(2), "end_date": _past(1)})
    assert code == 400 and "bad symbols" in body["error"]
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD"], "start_date": _past(5), "end_date": _past(2),
                           "asset_type": "space"})
    assert code == 400 and "asset_type" in body["error"]


def test_backfill_rejects_future_end(api):
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD"], "start_date": _past(2), "end_date": "2999-01-01"})
    assert code == 400 and "future" in body["error"]


def test_backfill_rejects_beyond_window(api, monkeypatch):
    monkeypatch.setattr(api.module, "BACKFILL_MAX_DAYS", 10)
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD"], "start_date": _past(20), "end_date": _past(15)})
    assert code == 400 and "backfill window" in body["error"]


# ---- /api/backfill happy path -----------------------------------------------

def test_backfill_creates_grid(api):
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD", "ETH-USD"],
                           "start_date": _past(4), "end_date": _past(2)})
    assert code == 202
    assert body["jobs_created"] == 6          # 2 symbols x 3 calendar days
    assert api.module.BATCHID.match(body["batch_id"])
    code, status = api.get(f"/api/backfill/{body['batch_id']}")
    assert code == 200
    assert status["total"] == 6
    assert status["queued"] == 6
    assert status["symbols"] == ["BTC-USD", "ETH-USD"]


def test_backfill_skips_in_flight_cells(api):
    # one interactive job occupies BTC-USD@day-3; the overlapping backfill cell must be skipped
    code, run = api.post("/api/run", {
        "symbol": "BTC-USD", "date": _past(3), "asset_type": "crypto"})
    assert code == 202
    code, body = api.post("/api/backfill",
                          {"symbols": ["BTC-USD"], "start_date": _past(4), "end_date": _past(2)})
    assert code == 202
    assert body["jobs_created"] == 2
    assert body["cells_skipped_in_flight"] == 1


def test_backfill_dedupe_409_on_same_cell(api):
    code, run = api.post("/api/run", {"symbol": "BTC-USD", "date": _today(api.module)})
    assert code == 202
    code, body = api.post("/api/run", {"symbol": "BTC-USD", "date": _today(api.module)})
    assert code == 409 and body["job_id"] == run["job_id"]


# ---- job filters ------------------------------------------------------------

def test_jobs_filtered_by_symbol_and_batch(api):
    api.post("/api/run", {"symbol": "BTC-USD", "date": _today(api.module)})
    _, bf = api.post("/api/backfill", {"symbols": ["ETH-USD"], "start_date": _past(2), "end_date": _past(1)})
    bid = bf["batch_id"]
    code, eth_jobs = api.get("/api/jobs?symbol=ETH-USD")
    assert code == 200
    assert all(j["symbol"] == "ETH-USD" for j in eth_jobs["jobs"]) and eth_jobs["jobs"]
    code, bjobs = api.get(f"/api/jobs?batch_id={bid}&limit=10")
    assert code == 200
    assert all(j["batch_id"] == bid for j in bjobs["jobs"]) and len(bjobs["jobs"]) == 2
    assert all(j["kind"] == "backfill" for j in bjobs["jobs"])


# ---- claim priority ---------------------------------------------------------

def test_interactive_job_claims_before_backfill(api):
    _, bf = api.post("/api/backfill", {"symbols": ["ETH-USD"], "start_date": _past(3), "end_date": _past(3)})
    code, run = api.post("/api/run", {"symbol": "BTC-USD", "date": _today(api.module)})
    claim = api.module.claim_next_job()
    assert claim["id"] == run["job_id"], "interactive job must claim before backfill"
    assert claim["kind"] == "interactive"
    # the batch job is still queued afterwards
    claim2 = api.module.claim_next_job()
    assert claim2["kind"] == "backfill"


def test_claim_returns_none_when_empty(api):
    assert api.module.claim_next_job() is None


# ---- batch verdict bundle ----------------------------------------------------

def test_bundle_serves_finished_and_pending_cells(api, monkeypatch):
    _, bf = api.post("/api/backfill", {"symbols": ["BTC-USD"], "start_date": _past(2), "end_date": _past(1)})
    bid = bf["batch_id"]
    m = api.module
    rows = m.db_rows("SELECT * FROM jobs WHERE batch_id=? ORDER BY analysis_date", (bid,))
    # finish the older cell by hand
    jdir = m.job_dir(rows[0]["id"])
    jdir.mkdir(parents=True, exist_ok=True)
    verdict = {"job_id": rows[0]["id"], "symbol": "BTC-USD",
               "analysis_date": rows[0]["analysis_date"], "run_ok": True, "rating": "Hold"}
    (jdir / "external_verdict.json").write_text(json.dumps(verdict))
    m.db_exec("UPDATE jobs SET state='finished', run_ok=1, rating='Hold' WHERE id=?", (rows[0]["id"],))

    code, bundle = api.get(f"/api/backfill/{bid}/verdicts.json")
    assert code == 200
    btc = bundle["verdicts"]["BTC-USD"]
    assert bundle["finished"] == 1 and bundle["queued"] == 1
    assert btc[rows[0]["analysis_date"]]["rating"] == "Hold"
    newer = btc[rows[1]["analysis_date"]]
    assert newer["run_ok"] is None and newer["state"] == "queued"
    assert "not finished" in newer["notes"]

    code, one = api.get(f"/api/backfill/{bid}/verdicts/btc-usd.json")   # case-insensitive access
    assert code == 200
    assert list(one["verdicts"].keys()) == ["BTC-USD"]


def test_unknown_batch_404(api):
    code, _ = api.get("/api/backfill/bf-20200101-deadbeef")
    assert code == 404


# ---- fail-closed path --------------------------------------------------------

def test_fail_verdict_carries_job_id(api):
    code, run = api.post("/api/run", {"symbol": "BTC-USD", "date": _today(api.module)})
    m = api.module
    jd = m.job_dir(run["job_id"])
    m.write_fail_verdict(jd, run["job_id"], "BTC-USD", _today(api.module), "FAILED: staged")
    code, art = api.get(f"/api/jobs/{run['job_id']}/verdict.json")
    assert code == 200
    assert art["job_id"] == run["job_id"]
    assert art["run_ok"] is False and art["rating"] is None


# ---- symbol-scoped latest ----------------------------------------------------

def _finish(api, jdir_jobid, symbol, rating, finished_at):
    m = api.module
    jd = m.job-dir(jdir_jobid) if False else m.job_dir(jdir_jobid)
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "external_verdict.json").write_text(json.dumps({
        "job_id": jdir_jobid, "symbol": symbol, "analysis_date": "2026-09-10",
        "run_ok": True, "rating": rating, "reference_close": 1.0}))
    (jd / "external_verdict.md").write_text("# report\n")
    m.db_exec("UPDATE jobs SET state='finished', run_ok=1, rating=?, finished_at=? WHERE id=?",
              (rating, finished_at, jdir_jobid))


def test_latest_for_symbol_never_crosses_symbols(api):
    _, tao = api.post("/api/run", {"symbol": "TAO-USD", "date": "2026-09-10"})
    _, btc = api.post("/api/run", {"symbol": "BTC-USD", "date": "2026-09-10"})
    # finish TAO first (older finished_at), BTC after: global latest is BTC,
    # the symbol route must still rig the desk the TAO artifact
    _finish(api, tao["job_id"], "TAO-USD", "Hold", "2026-09-10T01:00:00+00:00")
    _finish(api, btc["job_id"], "BTC-USD", "Buy", "2026-09-10T02:00:00+00:00")
    code, art = api.get("/api/verdict/latest/TAO-USD.json")
    assert code == 200
    assert art["symbol"] == "TAO-USD" and art["rating"] == "Hold"
    code, md = api.get_raw("/api/verdict/latest/BTC-USD.md")
    assert code == 200 and "report" in md


def test_latest_for_symbol_404_when_none(api):
    code, body = api.get("/api/verdict/latest/DOGE-USD.json")
    assert code == 404 and "DOGE-USD" in body["error"]


def test_latest_for_symbol_validates_symbol(api):
    code, body = api.get("/api/verdict/latest/TAO$.json")
    assert code == 400 and "bad symbol" in body["error"]
