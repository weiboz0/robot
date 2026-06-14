#!/usr/bin/env bash
# Local CI gate for rover-chatbot. Run before opening a PR (the autopilot-plan
# skill's PR gate calls this). Runs unit + integration tests; exits non-zero on
# the first failure so a red run blocks the PR.
#
#   ./ci-local.sh            # unit tests (default)
#   ./ci-local.sh --all      # unit + integration (integration needs the rover)
set -uo pipefail
DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"
RUN_INTEGRATION=0
[ "${1:-}" = "--all" ] && RUN_INTEGRATION=1
fail=0
step() { echo; echo "=== $* ==="; }

# ── unit: Go controller (with coverage) ─────────────────────────────────────
# COVERAGE_MIN: fail if statement coverage drops below this percent (default 70).
COVERAGE_MIN="${COVERAGE_MIN:-70}"
if [ -d rovercontrol ] && command -v go >/dev/null 2>&1; then
  step "go vet + test + coverage (rovercontrol)"
  if ( cd rovercontrol && go vet ./... && \
       go test -race -covermode=atomic -coverprofile=/tmp/rc-cov.out ./... ); then
    pct=$( (cd rovercontrol && go tool cover -func=/tmp/rc-cov.out) | awk '/^total:/{print $3}')
    echo "coverage: $pct (floor ${COVERAGE_MIN}%)  [unit only; hardware paths covered by --all integration]"
    num=${pct%\%}
    awk "BEGIN{exit !($num < $COVERAGE_MIN)}" && { echo "coverage below floor"; fail=1; }
  else
    fail=1
  fi
  step "go cross-compile (linux/arm64)"
  ( cd rovercontrol && GOOS=linux GOARCH=arm64 go build -o /tmp/rovercontrol-ci-arm64 . ) || fail=1
else
  echo "skip: go/rovercontrol not available"
fi

# ── unit: Python ────────────────────────────────────────────────────────────
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
if [ -n "$PY" ] && [ -d tests ]; then
  step "python unittest (tests/)"
  "$PY" -m unittest discover -s tests || fail=1
else
  echo "skip: python/tests not available"
fi

# ── integration (opt-in; runs the Go integration suite vs the live rover) ────
# The -tags integration tests hit the deployed controller's HTTP API and
# self-skip if it's unreachable, so they never fail CI when offline.
if [ "$RUN_INTEGRATION" = 1 ] && [ -d rovercontrol ] && command -v go >/dev/null 2>&1; then
  step "go integration tests (live rover, ROVER_URL=${ROVER_URL:-http://192.168.1.131:8080})"
  ( cd rovercontrol && ROVER_URL="${ROVER_URL:-http://192.168.1.131:8080}" \
      go test -tags integration -run Integration ./... ) || fail=1
fi

echo
if [ "$fail" = 0 ]; then echo "CI: PASS"; else echo "CI: FAIL"; fi
exit "$fail"
