#!/bin/sh
# fork-sync-test.sh - test gate executed by fork-sync against the REBASED worktree.
#
#   WORKTREE          path of the rebased checkout (set by fork-sync)
#   FORK_TEST_IMAGE   docker image to test in (pip installs the dev extra fresh);
#                     when unset, falls back to a local `pytest` on PATH.
#   FORK_TEST_NETWORK docker network for the test container (default: default)
#   PYTEST_ARGS       default: -m unit -q
set -eu
WT=${WORKTREE:-.}
PYTEST_ARGS=${PYTEST_ARGS:--m unit -q}

if [ -n "${FORK_TEST_IMAGE:-}" ] && command -v docker >/dev/null 2>&1; then
    exec docker run --rm \
        --network "${FORK_TEST_NETWORK:-default}" \
        --cap-drop ALL --security-opt no-new-privileges \
        --tmpfs /tmp:rw,exec,size=2g \
        -v "$WT:/src" -w /src \
        --entrypoint sh "$FORK_TEST_IMAGE" -c '
            set -e
            pip install -q --no-cache-dir --target /tmp/pydeps ".[dev]" >/dev/null
            PYTHONPATH=/tmp/pydeps:/src exec python -m pytest '"$PYTEST_ARGS"''
else
    cd "$WT"
    exec pytest $PYTEST_ARGS
fi
