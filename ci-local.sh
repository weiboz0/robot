#!/usr/bin/env bash
# Local CI gate for rover-chatbot. Run before opening a PR (the autopilot-plan
# skill's PR gate calls this). All-Python since the controller port: runs the
# full unittest suite (chatbot + controller safety tests). Exits non-zero on
# the first failure so a red run blocks the PR.
#
#   ./ci-local.sh            # unit tests
set -uo pipefail
DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"
fail=0
step() { echo; echo "=== $* ==="; }

PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
if [ -n "$PY" ] && [ -d tests ]; then
  step "python unittest (tests/)"
  "$PY" -m unittest discover -s tests || fail=1
  step "controller syntax + page integrity"
  "$PY" -c "import rovercontrold, rovercontrold_page; assert len(rovercontrold_page.PAGE) > 30000" || fail=1
else
  echo "skip: python/tests not available"
  fail=1
fi

echo
if [ "$fail" = 0 ]; then echo "CI: PASS"; else echo "CI: FAIL"; fi
exit "$fail"
