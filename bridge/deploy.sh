#!/bin/sh
# deploy.sh - interactive deploy/teardown for the containerized verdict-bridge stack.
#
# Safe to re-run: every prompt shows the current value; pressing ENTER keeps it.
# All answers persist in .deploy.env NEXT TO THIS SCRIPT (gitignored - it is your
# local config; the repo never learns your paths/IPs/tokens).
#
# What it manages on THIS host:
#   - the image (build-arg-pinned to your uid/gid, non-root by construction)
#   - a dedicated docker network (optionally with a pinned bridge name)
#   - an env file with your LLM/data keys (created 0600, never printed)
#   - a bearer-token file (generated 0600)
#   - systemd units: always-on verdict API + daily timer (skipped when not root)
#   - teardown with --teardown (units/containers; data kept unless you say otherwise)
#
# Nothing here is host-specific: defaults come from .deploy.env (or first-run
# sensible values), and every answer can be edited before each deploy.

set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(dirname -- "$HERE")
STATE_FILE="$REPO/.deploy.env"
IMAGE="${IMAGE:-tradingagents-bridge:latest}"
MODE="${1:-deploy}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ----- load/save state ------------------------------------------------------
[ -f "$STATE_FILE" ] && . "$STATE_FILE"
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
save_state() {
    umask 077
    {
    printf '# .deploy.env - local answers for bridge/deploy.sh (gitignored; never commit)\n'
    for k in PREFIX APP_UID APP_GID NETWORK_NAME NETWORK_BRIDGE_OPT ENV_FILE STORE_DIR \
             API_BIND API_PORT RUN_MODE RETENTION_DAYS JOB_TIMEOUT TIMER_CALENDAR INSTALL_SYSTEMD; do
        eval "v=\$$k"; printf '%s=%s\n' "$k" "$(sq "$v")"
    done
    } > "$STATE_FILE"
    chmod 600 "$STATE_FILE"
}

# ask VAR "prompt" "default-if-empty" -> leaves result in $VAR
ask() {
    _var=$1; _prompt=$2; _def=$3
    eval "_cur=\${$_var:-}"
    [ -n "$_cur" ] || _cur=$_def
    printf '%s [%s]: ' "$_prompt" "$_cur" >&2
    read -r _ans || true
    [ -n "$_ans" ] && _cur=$_ans
    eval "$_var=\$_cur"
}

confirm() { # confirm "question" default-yes(1)/no(0)
    _def="y/N"; [ "${2:-0}" = "1" ] && _def="Y/n"
    printf '%s [%s]: ' "$1" "$_def" >&2
    read -r _a || true
    case "$_a" in
        y|Y|yes) return 0 ;; n|N|no) return 1 ;;
        *) [ "${2:-0}" = "1" ] && return 0 || return 1 ;;
    esac
}

# ----- teardown ---------------------------------------------------------------
teardown() {
    say "== teardown =="
    if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1; then
        for u in verdict-api.service verdict.timer verdict.service; do
            systemctl disable --now "$u" 2>/dev/null || true
            rm -f "/etc/systemd/system/$u" 2>/dev/null || true
        done
        systemctl daemon-reload || true
    fi
    docker rm -f verdict-api verdict-run 2>/dev/null || true
    if confirm "also delete the image $IMAGE?" 0; then docker rmi "$IMAGE" || true; fi
    if confirm "also delete the named data volume (caches/checkpoints)?" 0; then
        docker volume rm "${VOLUME_NAME:-verdict-data}" || true
    fi
    say "teardown done (state file and env/token/store left on disk)"
    exit 0
}
[ "$MODE" = "--teardown" ] && teardown

# ----- prompts ----------------------------------------------------------------
say "== verdict-bridge deploy =="; say "Answers persist to $STATE_FILE (ENTER keeps the shown value)."
ask PREFIX "state dir for env/token/store" "${PREFIX:-$HOME/.verdict-bridge}"
[ -f "$ENV_FILE" ] && _="$(readlink -f "$ENV_FILE" 2>/dev/null || true)" || true
ask APP_UID "host uid containers run as" "${APP_UID:-$(id -u)}"
ask APP_GID "host gid containers run as" "${APP_GID:-$(id -g)}"
ask NETWORK_NAME "docker network for the stack" "${NETWORK_NAME:-verdict-net}"
ask NETWORK_BRIDGE_OPT "pin bridge interface name (blank = docker assigns)" "${NETWORK_BRIDGE_OPT:-}"
ask ENV_FILE "secrets env file (LLM + data keys; created 0600 if missing)" "${ENV_FILE:-$PREFIX/tradingagents.env}"
ask STORE_DIR "verdict store dir (history + latest)" "${STORE_DIR:-$PREFIX/verdict-store}"

say ""
say "Serving lock-down choices:"
say "  127.0.0.1     = loopback only (tightest; front it yourself with an ssh tunnel,"
say "                  or a mesh-network serve frontend, or a reverse proxy)"
say "  <interface IP> = bind one address only (e.g. a mesh/VPN interface address)"
say "  0.0.0.0       = every interface (NOT recommended; API has bearer auth but no TLS)"
ask API_BIND "API bind address" "${API_BIND:-127.0.0.1}"
if [ "$API_BIND" = "0.0.0.0" ]; then
    confirm "0.0.0.0 exposes the API on ALL interfaces - really continue?" 0 || die "aborted; pick a single-address bind"
fi
ask API_PORT "API port" "${API_PORT:-8877}"
ask RUN_MODE "run mode: api | timer | both" "${RUN_MODE:-both}"
case "$RUN_MODE" in api|timer|both) ;; *) die "run mode must be api|timer|both" ;; esac
ask RETENTION_DAYS "history retention (days)" "${RETENTION_DAYS:-90}"
ask TIMER_CALENDAR "OnCalendar for the timer (systemd syntax)" "${TIMER_CALENDAR:-Mon-Fri 07:00:00}"
ask JOB_TIMEOUT "per-job timeout (seconds)" "${JOB_TIMEOUT:-3900}"
if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1; then _sd=1; else _sd=0; fi
ask INSTALL_SYSTEMD "install+enable systemd units (1=yes 0=no)" "${INSTALL_SYSTEMD:-$_sd}"

TOKEN_FILE="$PREFIX/api-token"
VOLUME_NAME="verdict-data"
mkdir -p "$PREFIX" "$STORE_DIR"

# ----- prerequisites -----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"

# ----- secrets env file (create empty-permissioned; never echo values) --------
if [ ! -f "$ENV_FILE" ]; then
    umask 077
    cat > "$ENV_FILE" <<'EOF'
# TradingAgents runtime config for the bridge (mounted read-only into containers).
# Fill at least one LLM provider lane before runs can succeed:
TRADINGAGENTS_LLM_PROVIDER=openai_compatible
TRADINGAGENTS_LLM_BACKEND_URL=
OPENAI_COMPATIBLE_API_KEY=
TRADINGAGENTS_DEEP_THINK_LLM=
TRADINGAGENTS_QUICK_THINK_LLM=
TRADINGAGENTS_MAX_TOKENS=8192
# TRADINGAGENTS_LLM_FALLBACKS={"deep":[...],"quick":[...]}
# FRED_API_KEY=
EOF
    say "created $ENV_FILE (0600) - fill in your provider lane"
else
    chmod 600 "$ENV_FILE"
fi

# token
if [ ! -s "$PREFIX/api-token" ]; then
    umask 077
    (command -v openssl >/dev/null 2>&1 && openssl rand -hex 24 || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n') > "$PREFIX/api-token"
    printf '\n' >> "$PREFIX/api-token"
    say "generated bearer token at $TOKEN_FILE (0600)"
fi
chmod 600 "$TOKEN_FILE"

# ----- docker network ----------------------------------------------------------
if ! docker network inspect "$NETWORK_NAME" >/dev/null 2>&1; then
    if [ -n "$NETWORK_BRIDGE_OPT" ]; then
        docker network create --opt "com.docker.network.bridge.name=$NETWORK_BRIDGE_OPT" "$NETWORK_NAME" >/dev/null
    else
        docker network create "$NETWORK_NAME" >/dev/null
    fi
    say "created docker network $NETWORK_NAME"
fi

# ----- build ------------------------------------------------------------------
say "building $IMAGE (uid=$APP_UID gid=$APP_GID) ..."
if ! docker build --build-arg APP_UID="$APP_UID" --build-arg APP_GID="$APP_GID" -t "$IMAGE" "$REPO"; then
    say "direct build failed - retrying through a builder pinned to $NETWORK_NAME"
    say "(needed when the host firewall restricts container egress during build)"
    docker buildx rm verdict-builder >/dev/null 2>&1 || true
    docker buildx create --name verdict-builder --driver docker-container \
        --driver-opt network="$NETWORK_NAME" --use >/dev/null
    docker buildx inspect verdict-builder --bootstrap >/dev/null
    docker buildx build --builder verdict-builder --load \
        --build-arg APP_UID="$APP_UID" --build-arg APP_GID="$APP_GID" -t "$IMAGE" "$REPO"
fi

# ----- egress probe -------------------------------------------------------------
say "egress probe: HTTPS from a container on $NETWORK_NAME ..."
if docker run --rm --network "$NETWORK_NAME" --entrypoint python "$IMAGE" -c \
    "import urllib.request;urllib.request.urlopen('https://pypi.org/simple/',timeout=8)" >/dev/null 2>&1; then
    say "  ok"
else
    say "  WARNING: no HTTPS egress from containers on $NETWORK_NAME."
    say "  Runs need outbound TCP/443 to their LLM + market-data vendors."
    say "  Allow it in the host firewall for this bridge (and nothing broader than needed),"
    say "  then re-run: ./bridge/deploy.sh (ENTER keeps every stored answer)."
fi

# ----- systemd (root only) ------------------------------------------------------
render_api_unit() {
    cat <<EOF
[Unit]
Description=verdict API (containerized, uid $APP_UID)
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker rm -f verdict-api
ExecStart=/usr/bin/docker run --rm --name verdict-api \\
  --cap-drop ALL --security-opt no-new-privileges \\
  --network $NETWORK_NAME \\
  -p $API_BIND:$API_PORT:8877 \\
  --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m \\
  --tmpfs /home/appuser/.cache:rw,uid=$APP_UID,gid=$APP_GID,mode=0700,size=256m \\
  --pids-limit 512 --memory 8g \\
  -e VERDICT_API_TOKEN_FILE=/run/api-token \\
  -e VERDICT_API_STORE=/srv/verdicts \\
  -e VERDICT_API_DB=/home/appuser/.tradingagents/verdict-api.db \\
  -e VERDICT_API_RETENTION_DAYS=$RETENTION_DAYS \\
  -e VERDICT_API_JOB_TIMEOUT=$JOB_TIMEOUT \\
  --mount type=bind,src=$ENV_FILE,dst=/home/appuser/app/.env,readonly \\
  --mount type=bind,src=$TOKEN_FILE,dst=/run/api-token,readonly \\
  --mount type=volume,src=$VOLUME_NAME,dst=/home/appuser/.tradingagents \\
  --mount type=bind,src=$STORE_DIR,dst=/srv/verdicts \\
  --entrypoint python \\
  $IMAGE bridge/verdict_api.py
ExecStop=/usr/bin/docker stop verdict-api
StandardOutput=append:$PREFIX/api.log
StandardError=append:$PREFIX/api.log

[Install]
WantedBy=multi-user.target
EOF
}

render_run_service() {
    cat <<EOF
[Unit]
Description=daily verdict run (via verdict API)
After=network-online.target docker.service verdict-api.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$HERE/fire.sh http://$API_BIND:$API_PORT $TOKEN_FILE
TimeoutStartSec=65min
StandardOutput=append:$PREFIX/timer.log
StandardError=append:$PREFIX/timer.log
EOF
}

render_run_timer() {
    cat <<EOF
[Unit]
Description=daily verdict (07:00 weekdays local exchange time - edit to taste)

[Timer]
OnCalendar=$TIMER_CALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

if [ "$INSTALL_SYSTEMD" = "1" ]; then
    mkdir -p "$PREFIX"
    [ "$RUN_MODE" != "timer" ] && render_api_unit > /etc/systemd/system/verdict-api.service
    [ "$RUN_MODE" != "api" ] && { render_run_service > /etc/systemd/system/verdict.service
                                  render_run_timer  > /etc/systemd/system/verdict.timer; }
    systemctl daemon-reload
    [ "$RUN_MODE" != "timer" ] && systemctl enable --now verdict-api.service
    [ "$RUN_MODE" != "api" ] && systemctl enable --now verdict.timer
    say "systemd units installed/enabled"
else
    say "not root or opted out - systemd steps skipped. Manual run:"
    say "  docker run --rm -d --name verdict-api --cap-drop ALL --security-opt no-new-privileges \\"
    say "    --network $NETWORK_NAME -p $API_BIND:$API_PORT:8877 \\"
    say "    -e VERDICT_API_TOKEN_FILE=/run/api-token -e VERDICT_API_STORE=/srv/verdicts \\"
    say "    --mount type=bind,src=$ENV_FILE,dst=/home/appuser/app/.env,readonly \\"
    say "    --mount type=bind,src=$TOKEN_FILE,dst=/run/api-token,readonly \\"
    say "    --mount type=volume,src=$VOLUME_NAME,dst=/home/appuser/.tradingagents \\"
    say "    --mount type=bind,src=$STORE_DIR,dst=/srv/verdicts \\"
    say "    --entrypoint python $IMAGE bridge/verdict_api.py"
fi

save_state

# ----- smoke --------------------------------------------------------------------
if [ "$RUN_MODE" != "timer" ]; then
    sleep 2
    if curl -fsS -m 5 "http://$API_BIND:$API_PORT/healthz" >/dev/null 2>&1; then
        say "API live:  http://$API_BIND:$API_PORT  (bearer token: $TOKEN_FILE)"
        say "  trigger: curl -X POST -H \"Authorization: Bearer \$(cat $TOKEN_FILE)\" \\"
        say "           -H 'Content-Type: application/json' -d '{}' http://$API_BIND:$API_PORT/api/run"
        say "  latest:  curl -H \"Authorization: Bearer \$(cat $TOKEN_FILE)\" http://$API_BIND:$API_PORT/api/verdict/latest.json"
    else
        say "API not answering yet - check: docker logs verdict-api"
    fi
fi
say "deploy complete (answers saved to $STATE_FILE)"
