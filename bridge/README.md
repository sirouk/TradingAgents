# External-verdict bridge: TradingAgents -> tao-fleet

Runs one TradingAgents crypto analysis (default TAO-USD) and drops
`external_verdict.json` + `external_verdict.md` into the tao-fleet workspace,
native to the fleet's files+DM cadence. The JSON is ALWAYS written; on failure it
carries run_ok=false + rating=null so Decision-Council gating can FAIL_CLOSED.
REVIEW = "no opinion today". Advisory only — no authority over draft_authorization.

- bridge/tao_verdict.py         the runner (venv python, .env auto-loaded)
- bridge/tao-verdict.service    systemd unit (flock + 65min guard)
- bridge/tao-verdict.timer      Mon-Fri 07:00:00 America/New_York, Persistent

Install/ref: sudo cp bridge/tao-verdict.* /etc/systemd/system &&
systemctl daemon-reload && systemctl enable --now tao-verdict.timer
Manual fire: systemctl start tao-verdict.service
Logs: /root/TradingAgents/bridge-logs/service.log
