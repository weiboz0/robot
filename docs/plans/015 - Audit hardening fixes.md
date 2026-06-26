# 015 - Audit hardening fixes

## Goal

Fix the real bugs found by a two-front static audit (Go controller + Python
chatbot/ops), validated by a codex + glm crosscheck. Make the chatbot REPL
crash-proof and harden a handful of smaller correctness issues across the stack.
No new features; no behavioural change to the happy path.

## How the fix set was chosen (this section IS the plan-review gate)

Two independent Claude audits produced ~20 candidate findings; codex (GPT-5.5)
and glm-5.1 then triaged each as REAL / FALSE-POSITIVE, corrected severities, and
flagged misses. Both crosscheckers agreed on the set below. Notable outcomes:
- **P12 (Dobot missing `\n`): FALSE-POSITIVE** — the MG400 TCP protocol parses on
  `)`; the official client sends no newline. **Excluded** (adding `\n` is risky).
- **G2 (signal/shutdown): both said INCLUDE, not defer** — stopping wheels on
  exit is a *stop*, not a move (safe), and prevents a rover holding its last
  command after the controller is killed.
- **Missed by the audits, added here:** the `$dobot` path in `main()` is also
  unguarded; message-history trimming must happen at **user-turn boundaries** so a
  `tool` message is never orphaned from its `assistant.tool_calls`.
- Deferred (touch the live binary more deeply / bigger refactors / low value):
  G1 serial-liveness (write-error latch is ineffective on `/dev/ttyAMA0`; real
  liveness needs re-enabling feedback reads), G5 serial-write-under-lock,
  G6 splitFrames EOF copy/cap, G7 gamepad-vs-cap unification.

## Fixes

### Python — chatbot robustness (`agent_chat.py`)
- **P1+P2+`$dobot`**: a REPL must never die on one bad command. (a) `rover_command`
  gets a final `except Exception as e: return f"error: {e}"`. (b) Wrap each loop
  iteration's command handling (LLM turn AND `$dobot`/`$rover` dispatch) so any
  exception prints and continues to the next prompt instead of exiting `main()`.
- **P3**: trim `messages` to system + the last N **at the user-turn boundary**
  (before appending the next user message), snapping the kept window's start to a
  `user` message so a `tool`/`assistant.tool_calls` pair is never split.
- **P4**: store assistant `content` as `None` (not `""`) on tool-call-only turns —
  some OpenAI-compatible providers reject `content: ""`.
- **P5**: on malformed tool-call-arguments JSON, return a structured
  `"error: arguments were not valid JSON: <raw>"` as the tool result so the model
  can self-correct (instead of a silent `{}` → cryptic KeyError).

### Python — clients / backend / ops
- **P6 (LOW)**: wrap `urlopen().read()` in `with` in `rovercontrol_client._post`,
  `rover_client._post`, `rover_client._send_json`.
- **P8 (LOW)**: `RoverCtl.status()` http backend → `serial.up = None` (unknown),
  not hard-coded `True`.
- **P9 (LOW)**: `rover_direct.stop_http_service` checks `pkill`'s return code (and
  re-probes) before reporting success.
- **P10 (LOW)**: `llm_config.load_dotenv` strips a leading `export ` from keys.
- **P11 (MED)**: `dobot.Dobot.__init__` closes the dashboard socket if the motion
  socket connect fails (no FD leak).
- **P13 (LOW)**: `install.sh` ALIASES loop skips (with a warning) a missing target
  instead of creating a dangling symlink.

### Go controller (`rovercontrol.go`)
- **G3 (LOW/MED)**: `floatParam` rejects non-finite (`NaN`/`Inf`) values, so
  `/speed?cap=NaN` can't poison the shared cap and `/drive?l=NaN` can't bump the
  movement generation with a value `json.Marshal` then chokes on.
- **G4 (LOW)**: do the read-compute-write under a single held lock in
  `toggleHead`/`toggleBase` and `CameraAim.nudge` (no lost updates on concurrent
  toggles/nudges).
- **G2 (MED, safety)**: `signal.NotifyContext(SIGINT,SIGTERM)`; on shutdown stop
  the wheels, close the serial link, and `srv.Shutdown(ctx)` instead of
  `log.Fatal`. Stopping wheels on exit is safe (a stop, not a move).

## Testing
- Python (`tests/`, fakes — no hardware): rover_command swallows a raising
  backend; message-trim keeps system + boundary-snapped tail and never orphans a
  tool message; assistant content stored as None on tool-only turns; bad-JSON
  tool args → structured error; status() http serial.up is None; llm_config
  parses `export K=V`; dobot closes dash on move-connect failure (fake sockets).
- Go (`rovercontrol_*_test.go`): `floatParam`/handler rejects NaN/Inf; toggle
  alternates correctly; a `stopOnShutdown`-style helper stops wheels + closes the
  link. (Signal delivery itself is not unit-tested; the shutdown helper is.)
- `./ci-local.sh` must pass (Go race + coverage ≥70, Python unittest).

## Deploy
- Python fixes → rover via `git pull` (no rebuild).
- Go fixes → cross-compile `GOOS=linux GOARCH=arm64`, rsync the binary, restart
  the controller. Restart sends no motion; verify `/healthz` after. No driving.

## Risks
- Broad `except` in the REPL loop could mask a real bug — acceptable for a REPL;
  the error is printed, not swallowed silently.
- Message trimming could drop context the user expected — mitigated by a generous
  N and trimming only whole turns.
- Restarting the live controller is the only outward action; it is safe (no
  motion on start) and verified via `/healthz`. Cat-safety: no drive/gimbal
  command is issued at any point; all tests use fakes.

## Stages
1. Python agent_chat hardening (P1–P5) + tests.
2. Python clients/backend/ops (P6,P8,P9,P10,P11,P13) + tests.
3. Go controller (G3,G4,G2) + tests.
4. ci-local, 3-way code review, PR, merge.
5. Deploy (pull on rover; rebuild+restart controller); verify /healthz.

## Reviews

### Plan review (codex + glm crosscheck triage) — both validated the fix set
Recorded above ("How the fix set was chosen"): both confirmed REAL vs
FALSE-POSITIVE, corrected P6 MED→LOW, and both moved G2 into scope. No blocking
disagreement remained.

### Code review (Opus + codex + glm) — APPROVE after one blocker resolved

- **Opus: APPROVE** — verified no camera deadlock (lock order strictly
  a.mu→r.mu), no lost light/camera updates, safe shutdown order, REPL guards
  spare Ctrl-C, trim_history never orphans a tool message. Nit: a near-nil
  shutdown drive race.
- **glm: APPROVE** — same conclusions; flagged the lights PWM-write-after-unlock
  ordering as a non-blocking nit.
- **codex: REQUEST-CHANGES (1 BLOCKING)** — `updateLights` committed state then
  wrote PWM after releasing `lightMu`, so two concurrent toggles could leave
  hardware disagreeing with state. **Resolved**: the PWM write now happens under
  `lightMu` (defer Unlock), serializing the write with the commit. codex
  re-verified: lightMu→r.mu has no reverse path (no deadlock), issue resolved.
- Also folded in: shutdown uses `move.doEstop()` (latch e-stop) instead of
  `stop()` so a `/drive` racing the shutdown window is refused (Opus nit);
  `openSerialWithRetry` checks `ctx.Err()` before publishing a link so a late
  connect can't escape `closeLink()` (codex nit).

## Post-execution report

**Implemented** all planned fixes: Python REPL hardening (P1–P5), Python
clients/backend/ops (P6,P8,P9,P10,P11,P13), Go controller (G2,G3,G4). P12
correctly excluded as a false-positive (Dobot needs no `\n`). Added 7 Python
tests + 6 Go tests.

**Deviations from plan:** (1) `updateLights` and `CameraAim` now hold their lock
across the (camera/LED, ms-bounded) serial write to fully serialize hardware with
state — stronger than the plan's "write after unlock". (2) Shutdown uses
`doEstop()` not `stop()` to close a drive race. (3) Added an `openSerialWithRetry`
ctx guard. All three came out of the code-review gate.

**Tests/CI:** `ci-local.sh` PASS — Go race-clean, coverage 73.8% (≥70 floor),
67 Python unit tests. No hardware touched; all tests use fakes (cat-safety).

**Deferred (proposed, not done):** G1 serial-liveness (needs re-enabling feedback
reads — a real behavioural change), G5 serial-write-off-lock, G6 splitFrames
EOF/cap, G7 gamepad-vs-cap unification.
