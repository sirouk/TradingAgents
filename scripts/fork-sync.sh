#!/bin/sh
# fork-sync.sh - keep a fork's main tracking upstream (fast-forward only) and
# replay the fork's customization branch onto each new upstream state.
#
# Pattern: main = upstream mirror (never rewritten, FF only). Delta branch =
# your customization, re-applied on top via rebase. A patch series is
# EXPORTED as an artifact after every sync (audit/apply-elsewhere), it is
# never the mechanism.
#
# Default is DRY-RUN (report only). --apply moves refs; pushes only when the
# push credential permits it (gracefully deferred otherwise).
#
# Config (all env; .fork-sync.env next to the repo state dir is sourced):
#   FORK_UPSTREAM_URL   https read URL of upstream       [origin of truth]
#   FORK_ORIGIN         remote name for the fork         [origin]
#   MAIN_BRANCH         fork main mirroring upstream     [main]
#   DELTA_BRANCH        customization branch             [feat/binance-crypto-vendor]
#   FORK_BASE_REF       rebase anchor                    [upstream/main tip]
#   PUSH                auto|never                       [auto]
#   STATE_DIR           logs + status                    [<repo>/.fork-sync]
#   TEST_CMD            gate run in the rebased worktree ["$WORKTREE" is set;
#                                                    empty = skip]
#
# Exit codes: 0 ok/no-op, 3 rebase conflict, 4 test failure, 5 push deferred/denied.

set -eu
export GIT_TERMINAL_PROMPT=${GIT_TERMINAL_PROMPT:-0}   # never hang waiting for creds inside services
MODE=report
[ "${1:-}" = "--apply" ] && MODE=apply

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO"

FORK_UPSTREAM_URL=${FORK_UPSTREAM_URL:-}
FORK_ORIGIN=${FORK_ORIGIN:-origin}
MAIN_BRANCH=${MAIN_BRANCH:-main}
DELTA_BRANCH=${DELTA_BRANCH:-feat/binance-crypto-vendor}
PUSH=${PUSH:-auto}
STATE_DIR=${STATE_DIR:-$REPO/.fork-sync}
TEST_CMD=${TEST_CMD:-}
[ -f "$STATE_DIR/.fork-sync.env" ] && . "$STATE_DIR/.fork-sync.env"
export FORK_TEST_IMAGE FORK_TEST_NETWORK PYTEST_ARGS 2>/dev/null || true
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/sync.log"
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
exec >> "$LOG" 2>&1
say() { printf '%s %s\n' "$STAMP" "$*"; }

say "=== fork-sync ($MODE) ==="

git remote get-url upstream >/dev/null 2>&1 || {
    [ -n "$FORK_UPSTREAM_URL" ] || { say "ERROR: no 'upstream' remote and FORK_UPSTREAM_URL unset"; exit 1; }
    git remote add upstream "$FORK_UPSTREAM_URL"
}
git fetch -q upstream --tags
git fetch -q "$FORK_ORIGIN" --tags 2>/dev/null || true

resolve() { # prefer local branch, then fork remote, then upstream
    git rev-parse --verify -q "refs/heads/$1" 2>/dev/null     || git rev-parse --verify -q "refs/remotes/$FORK_ORIGIN/$1" 2>/dev/null     || git rev-parse --verify -q "refs/remotes/upstream/$1"
}
UP_TIP=$(git rev-parse "refs/remotes/upstream/$MAIN_BRANCH")
BASE=$(git rev-parse "${FORK_BASE_REF:-refs/remotes/upstream/$MAIN_BRANCH}")
DELTA_TIP=$(resolve "$DELTA_BRANCH")
MAIN_TIP=$(resolve "$MAIN_BRANCH")
say "upstream/$MAIN_BRANCH=$UP_TIP delta=$DELTA_TIP base=$BASE"

NEW_MAIN=no
[ "$MAIN_TIP" != "$UP_TIP" ] && {
    git merge-base --is-ancestor "$MAIN_TIP" "$UP_TIP" \
        && { NEW_MAIN=yes; say "main can fast-forward: $MAIN_TIP -> $UP_TIP"; } \
        || { say "ERROR: fork main has commits not on upstream/main; FF-only policy aborts"; exit 2; }
}

# already-on-base check: delta needs a rebase only when BASE has commits the
# delta does not yet contain (i.e. upstream advanced past the delta's ancestry).
git merge-base --is-ancestor "$BASE" "$DELTA_BRANCH" && REBASE_NEEDED=no || REBASE_NEEDED=yes
say "main-ff: $NEW_MAIN  delta-rebase: $REBASE_NEEDED"
[ "$MODE" = report ] && { say "dry-run done"; exit 0; }
[ "$NEW_MAIN" = no ] && [ "$REBASE_NEEDED" = no ] && { say "nothing to do"; exit 0; }

# ---- rebase the delta branch in an isolated worktree ------------------------
NEW_DELTA=$DELTA_TIP
if [ "$REBASE_NEEDED" = yes ]; then
    WT="$STATE_DIR/worktree"
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
    git worktree add --force --detach "$WT" "$DELTA_BRANCH" >/dev/null
    MB=$(git merge-base "$DELTA_BRANCH" "$BASE")
    if ! git -C "$WT" rebase --quiet --onto "$BASE" "$MB"; then
        git -C "$WT" rebase --abort || true
        git worktree remove --force "$WT" >/dev/null 2>&1 || true
        say "CONFLICT rebasing $DELTA_BRANCH onto $BASE - refs untouched; manual fix required"
        echo '{"state":"conflict","ts":"'"$STAMP"'","base":"'"$BASE"'","delta":"'"$DELTA_TIP"'"}' > "$STATE_DIR/status.json"
        exit 3
    fi
    NEW_DELTA=$(git -C "$WT" rev-parse HEAD)
    say "rebase clean: $DELTA_TIP -> $NEW_DELTA ($(git -C "$WT" rev-list --count "$BASE"..HEAD) commits replayed)"

    if [ -n "$TEST_CMD" ]; then
        say "running test gate..."
        if ( cd "$WT" && WORKTREE="$WT" sh -c "$TEST_CMD" ); then
            say "test gate: PASS"
        else
            say "test gate: FAIL - refs untouched"
            git worktree remove --force "$WT" >/dev/null 2>&1 || true
            echo '{"state":"tests-failed","ts":"'"$STAMP"'","candidate":"'"$NEW_DELTA"'"}' > "$STATE_DIR/status.json"
            exit 4
        fi
    fi
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
fi

# ---- move refs ---------------------------------------------------------------
[ "$NEW_MAIN" = yes ] && git update-ref "refs/heads/$MAIN_BRANCH" "$UP_TIP"
[ "$NEW_DELTA" != "$DELTA_TIP" ] && git update-ref "refs/heads/$DELTA_BRANCH" "$NEW_DELTA"
git format-patch --stdout "$BASE".."$NEW_DELTA" > "$STATE_DIR/delta-series.patch" 2>/dev/null || true
say "refs updated: main=$(git rev-parse "$MAIN_BRANCH") delta=$(git rev-parse "$DELTA_BRANCH")"

# ---- push (best effort) ------------------------------------------------------
if [ "$PUSH" != never ]; then
    PUSH_OK=yes
    [ "$NEW_MAIN" = yes ] && git push -q "$FORK_ORIGIN" "$MAIN_BRANCH:$MAIN_BRANCH" || PUSH_OK=$?
    [ "$PUSH_OK" = yes ] || [ "$PUSH_OK" = 0 ] || PUSH_OK=no
    [ "$NEW_DELTA" != "$DELTA_TIP" ] && {
        git push -q --force-with-lease "$FORK_ORIGIN" "$DELTA_BRANCH:$DELTA_BRANCH" || PUSH_OK=no; }
    git push -q "$FORK_ORIGIN" --tags 2>/dev/null || true
    if [ "$PUSH_OK" != yes ]; then
        say "PUSH-FAILED-DEFERRED (local refs updated; will retry next run; check push credential)"
        echo '{"state":"push-deferred","ts":"'"$STAMP"'"}' > "$STATE_DIR/status.json"
        exit 5
    fi
    say "pushed"
fi

echo '{"state":"ok","ts":"'"$STAMP"'","main":"'"$(git rev-parse "$MAIN_BRANCH")"'","delta":"'"$(git rev-parse "$DELTA_BRANCH")"'"}' > "$STATE_DIR/status.json"
say "=== done ==="
