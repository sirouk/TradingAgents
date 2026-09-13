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
- On failure ALL verdict fields are null (rating, stop_loss, sizing, summary,
  reference_close). A failed run emits no numbers at all — this is contract,
  not an oversight: plausible-looking data on a failed run is worse than none.
- queue `position` counts queued jobs only; the running job is not counted, so
  position=1 means "next to run after the current run finishes".
- `generated_at` is the host's UTC clock stamp of when the artifact was written.
  For staleness gating record your own fetched_at and compute age from that.

Logs go where you point them (StandardOutput=append:<path> in the unit, or
docker logs verdict-api). No secrets belong in this repo: keys live only in the
env file the runner mounts (0600), and deploy.sh never echoes them.

## Backfill & batch surface (one API for realtime + history)

The same API serves realtime and historical requests; both lanes share the
queue and the store, with two fairness and integrity rules:

- POST /api/backfill {"symbols": [...], "start_date", "end_date", "asset_type"}
  enqueues one verdict per symbol x calendar-day cell (depth capped by
  VERDICT_API_BACKFILL_MAX_DAYS, default 180) and returns a batch_id. Cells
  already in-flight across either lane are skipped (dedupe is global, by
  symbol+date). A run for today is just a realtime run.
- Fairness: workers (VERDICT_API_WORKERS, default 2) always claim interactive
  /api/run jobs before backfill jobs, so a 1,400-cell sweep can never starve
  a realtime request.
- Vintage integrity: any run whose analysis date is in the past pins the
  prediction-market vendor to that date's CLOB price history
  (~hourly-resolution point-in-time odds; volume figures omitted as
  retrospective-by-construction), and writes into a batch-scoped memory log
  instead of the realtime lane's memory. Historical runs never see today's
  odds or today's verdicts.
- Readback: GET /api/backfill/{id} (spec + state counts),
  /api/backfill/{id}/verdicts.json (every cell keyed symbol -> date) or
  /verdicts/{symbol}.json (one symbol). 90-day retention (VERDICT_API_RETENTION_DAYS)
  applies to artifacts identically in both lanes.
