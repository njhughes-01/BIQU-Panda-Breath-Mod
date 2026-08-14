#!/usr/bin/env bash
# Run the Panda Breath Mod test suite FULLY containerized: build the test image
# (all Python deps baked in) and run pytest inside the compose test stack, then
# tear the stack down on exit (pass or fail). Never run bare pytest on the host.
#
# The Docker engine is chosen by whoever invokes this: the daemon test runner
# exports DOCKER_HOST from its committed test-docker-hosts.json, so this script
# carries no host config of its own. Run it by hand with `DOCKER_HOST=... bash
# run-tests.sh` (or your local Docker) when iterating.
set -euo pipefail

# ── Which Docker engine runs this suite ───────────────────────────────────────
# One source of truth, shared with every other repo: the daemon's committed
# test-docker-hosts.json. This script used to take whatever DOCKER_HOST happened
# to be in the environment. Launched by the daemon's test runner that is correct,
# because the runner exports it — but run by hand, which is what the docs tell you
# to do, it silently built the whole stack on the Mac's Docker instead. A stray
# members-app container was found still running on the Mac after 3 days, and the
# Mac had accumulated 40 GB of test images against 14 GB of free disk.
#
# An explicit DOCKER_HOST still wins, so `DOCKER_HOST=... bash run-tests.sh`
# remains available for deliberate local iteration. A missing or unreadable file
# falls back to the same default the daemon uses rather than silently going local.
if [ -z "${DOCKER_HOST:-}" ]; then
  TEST_DOCKER_HOSTS_FILE="${TEST_DOCKER_HOSTS_FILE:-$HOME/claude-daemon/test-docker-hosts.json}"
  RESOLVED_DOCKER_HOST="$(node -e '
    const fs = require("fs");
    try {
      const j = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      const h = (j.hosts || []).find(Boolean) || "";
      process.stdout.write(/^(local|mac|localhost|default)$/i.test(h) ? "" : h);
    } catch { process.stdout.write(""); }
  ' "$TEST_DOCKER_HOSTS_FILE" 2>/dev/null || true)"
  if [ -n "$RESOLVED_DOCKER_HOST" ]; then
    DOCKER_HOST_SOURCE="$TEST_DOCKER_HOSTS_FILE"
  elif [ -n "${TEST_DOCKER_HOSTS:-}" ]; then
    DOCKER_HOST_SOURCE="TEST_DOCKER_HOSTS env"
  else
    DOCKER_HOST_SOURCE="built-in default"
  fi
  DOCKER_HOST="${RESOLVED_DOCKER_HOST:-${TEST_DOCKER_HOSTS:-ssh://proxmoxdocker}}"
  export DOCKER_HOST
  echo "=== Docker engine: $DOCKER_HOST (from $DOCKER_HOST_SOURCE) ==="
else
  echo "=== Docker engine: $DOCKER_HOST (explicit DOCKER_HOST) ==="
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-panda-breath-test-$$}"
export COMPOSE_PROJECT_NAME

COMPOSE=(docker-compose -p "$COMPOSE_PROJECT_NAME" -f "$SCRIPT_DIR/docker-compose.test.yml")

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans 2>/dev/null || true; }
trap cleanup EXIT

"${COMPOSE[@]}" build
# `run --rm` returns the test container's own exit code, so a pytest failure
# propagates straight out of this script.
"${COMPOSE[@]}" run --rm tests
