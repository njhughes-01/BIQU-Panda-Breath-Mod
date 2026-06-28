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
