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

# ── unit: Go controller ─────────────────────────────────────────────────────
if [ -d rovercontrol ] && command -v go >/dev/null 2>&1; then
  step "go vet + test (rovercontrol)"
  ( cd rovercontrol && go vet ./... && go test -race ./... ) || fail=1
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

# ── integration (opt-in; needs the rover reachable) ─────────────────────────
if [ "$RUN_INTEGRATION" = 1 ]; then
  step "integration: rover reachable + controller health"
  if ssh -o ConnectTimeout=6 rover true 2>/dev/null; then
    # rovercontrol must be answering on :8080 with serial+camera up
    curl -fsS --max-time 5 "http://192.168.1.131:8080/healthz" \
      | grep -q '"up":true' || { echo "controller /healthz not healthy"; fail=1; }
  else
    echo "rover unreachable — integration skipped (not a failure)"
  fi
fi

echo
if [ "$fail" = 0 ]; then echo "CI: PASS"; else echo "CI: FAIL"; fi
exit "$fail"
