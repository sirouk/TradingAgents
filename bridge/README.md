# External-verdict bridge: TradingAgents -> downstream workspace

Runs one TradingAgents crypto analysis (default TAO-USD) and drops
`external_verdict.json` + `external_verdict.md` into a target workspace,
native to a files+cadence consumption contract. The JSON is ALWAYS written; on
failure it carries run_ok=false + rating=null so downstream gating can
FAIL_CLOSED. REVIEW = "no opinion today". Advisory only — no authority.

Components:

- bridge/tao_verdict.py       the runner (location independent; .env auto-loaded
                              from the repo root; --target-dir chooses the drop)
- bridge/verdict_api.py       authenticated HTTP front-end: POST /api/run queues a
                              job, GET endpoints serve job status, artifacts and the
                              latest verdict; 90-day history with an hourly janitor;
                              one run at a time; restart reaps stale jobs to failed
                              (fail-closed)
- bridge/deploy.sh            interactive deploy/teardown (image, docker network,
                              secrets/env scaffolding, bearer token, systemd units,
                              egress + health smoke checks). Answers persist in the
                              gitignored .deploy.env — ENTER keeps every stored value.
- bridge/fire.sh              one-line enqueue shim used by the timer unit
- bridge/tao-verdict.service  reference systemd unit for non-containerized hosts
- bridge/tao-verdict.timer    Mon-Fri 07:00:00 America/New_York, Persistent

Quick start (containerized, recommended):

    ./bridge/deploy.sh            # answer or ENTER through; re-run anytime, idempotent
    ./bridge/deploy.sh --teardown

The API token lives at <prefix>/api-token (0600). Serve exposure is chosen at
deploy time: 127.0.0.1 (default; front with your tunnel/mesh of choice) or a
single interface IP. 0.0.0.0 requires explicit confirmation.

Bare-metal alternative (no containers):

    python -m venv .venv && .venv/bin/pip install .
    sudo cp bridge/tao-verdict.* /etc/systemd/system   # edit paths to your checkout
    systemctl daemon-reload && systemctl enable --now tao-verdict.timer
    systemctl start tao-verdict.service                # manual fire


Artifact contract (what consumers can rely on):

- `job_id` is embedded in every JSON artifact (first field), including the
  synthesized run_ok=false failures, so any stored file is self-identifying.
  `latest.json` carries the job_id of the run that produced it.
- `date` in POST /api/run is honoured end to end (`--date` -> propagate), so
  backfill works: request a past `YYYY-MM-DD` to re-analyse that session.
- `stop_loss` / `position_sizing` are surfaced verbatim from the PM decision
  (falling back to the trader plan). On a Hold/maintain call the PM commonly
  prints no labelled stop, so **null = "no stop stated", never a parser failure**
  — consumers must not read a null stop as a signal.
- `generated_at` is the host's UTC clock stamp of when the artifact was written.
  For staleness gating record your own fetched_at and compute age from that.

Logs go where you point them (StandardOutput=append:<path> in the unit, or
docker logs verdict-api). No secrets belong in this repo: keys live only in the
env file the runner mounts (0600), and deploy.sh never echoes them.
