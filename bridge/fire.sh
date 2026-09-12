#!/bin/sh
# fire.sh - enqueue one verdict run through the local verdict API.
# Used by the timer unit; equivalent to a manual POST from any allowed client.
#   fire.sh [base-url] [token-file]
BASE="${1:-http://127.0.0.1:8877}"
TOKEN_FILE="${2:?token file path required}"
exec curl -fsS -m 20 -X POST \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -H "Content-Type: application/json" \
  -d '{}' \
  "$BASE/api/run"
