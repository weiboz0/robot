# 018 - Web command sequencer ("scratch" for the rover)

## Goal

Make the controller's built-in web page let you (1) press **Enter** to send a
command, (2) **click a command** in the Commands menu to load it into the box,
and (3) build a **program** — an ordered stack of commands you can reorder, save,
and **Run with repeat + a gap**, instead of typing them one by one. Front-end
only (the `htmlPage` constant); every step maps to an existing clamped/watchdog'd
HTTP endpoint — no new Go handler, no raw serial.

## Process note
This plan is written after an initial implementation + a codex/glm code-review
crosscheck (the plan→plan-review gate was skipped and is being corrected). The 3
blocking findings from that review are folded into the design below and will be
implemented before merge; the plan-review gate re-validates the corrected design.

## Design

- **Enter → send**: the command box stays wrapped in a `<form onsubmit="runCmd();
  return false">` (already shipped in #26); a fresh deploy also clears any cached
  page. Send button is `type=submit`; other buttons are `type=button`.
- **Clickable Commands menu**: each help-table command cell gets `onclick=
  "pick('<template>')"` which loads an editable template (e.g. `drive 0.2 0.2`)
  into the box and focuses it.
- **Program engine** (client-side JS):
  - `parseCmd(raw)` → `{cmd,path}` or `{error}` (refactored out of `runCmd`, shared).
  - `sendCommand(raw)` → `Promise<bool>` (awaitable; updates the status line).
  - `prog=[]` (array of raw command strings); `addStep` (validates via `parseCmd`
    before pushing), `rm`, `mv` (reorder), `clearProg`, `renderProg` (steps shown
    via `textContent` — no XSS).
  - `runProgram()` runs `prog` `reps` times with a `gap`, highlighting the active
    step; `stopProgram()` halts it and `/stop`s the wheels.
  - Save/load **named programs** in `localStorage` (`roverprog:<name>`); the load
    `<select>` is built with `createElement`/`textContent` (no XSS).

### Safety + the 3 code-review fixes (BLOCKING → in the design)
1. **Run-generation token, ownership-guarded (codex/glm #1 + codex plan blocker).**
   `stopProgram` setting a shared `running=false` while the old loop sleeps lets a
   restart interleave two loops. Fix: `const my=++runGen` at run start; check
   `my!==runGen` after every `await` and bail; `stopProgram` bumps `runGen`.
   **Cleanup is ownership-guarded and idempotent**: the `finally` only mutates
   shared UI/rover state (`running=false`, clear highlight, `/stop`, status) **if
   `my===runGen`** — so a superseded/stopped old run NEVER stomps the new run's
   state or re-posts `/stop`; and a new generation is unaffected by the old one's
   `finally`. `stopProgram` itself sets `running=false`, clears the highlight,
   `/stop`s, and sets status "stopped".
2. **Snapshot the program + `try/finally` (glm #2).** The loop iterates a
   `const steps=[...prog]` copy, not the live global — else `clearProg`/`loadProg`/
   `rm` mid-run makes `prog[i]` undefined → throw → `running` stuck and the safety
   `/stop` skipped. The `try/finally` guarantees the ownership-guarded cleanup.
3. **Wait the motion duration, not just the gap (codex/glm #3).** `/drive`
   (0.5 s watchdog) and `/move_*` (up to `ms`) return immediately, so a short gap
   starts the next step mid-motion. Fix: wait `max(gap, motionMs(step))` where
   `motionMs` returns 500 for `drive`, the `ms` arg **clamped to the server's
   0..5000** (default 400) for `move_*`, else 0 — so a step's motion always
   finishes before the next.
4. **A failed step aborts the run (codex plan).** `sendCommand` returns
   `Promise<bool>`; if a step fails (HTTP error / non-OK / parse error), the run
   loop stops, `/stop`s, and surfaces the failed step — so the displayed program
   can't diverge from rover state.
5. **Double-run contract (codex plan):** `runProgram()` is **ignored while a run is
   active** (`if(running)return`) — Stop, then Run, to restart. Stop is always
   available (never disabled) and idempotent.
- Server-side safety is unchanged (clamps, watchdog, e-stop). Running a program is
  exactly the same as typing those commands; Stop + `/stop` + the big E-STOP
  remain. Repeat is clamped 1..1000; gap 0..10 s.

## Scope / non-goals
- Whole-program repeat only (a "repeat N times" for the sequence). Nested/block
  loops (true Scratch blocks) are a possible later extension — noted, not built.
- Programs are per-browser (localStorage), not shared across devices.
- The sequencer uses the website command vocabulary; the separate chatbot/website
  name-parity task is deferred (tracked separately).

## Testing
- Go `rovercontrol_webui_test.go`: `GET /` contains the new controls
  (`onsubmit=runCmd`, `pick(`, `addStep(`, `runProgram(`, `id="program"`,
  `roverprog:`) and every command-box target route is non-404 (existing test).
- The JS run-loop logic (run-gen token, snapshot copy, motionMs) is client-side
  and not unit-testable in Go — it is covered by the code-review crosscheck and a
  manual post-deploy check. No motion is issued during dev/test.
- `./ci-local.sh` green.

## Deploy
Rebuild arm64, rsync, restart the controller, verify `GET /` has the new controls.
No motion sent during deploy.

## Reviews
### Plan review (Opus + codex) — APPROVE (codex request-changes resolved)
Both verified front-end-only (all commands map to existing routes), E-STOP is a
real server-side backstop, XSS via textContent is sound, and motionMs constants
match the server (watchdogTTL=500, nudge default 400). Blocking item (both): the
`try/finally` cleanup must be generation-guarded (`if(my===runGen)`) — built exactly
so. Nits folded in: couple `500` to `watchdogTTL` (comment), floor step delay
(MIN_STEP_MS), and a UI note that Stop (not E-STOP) ends a program.

### Code review (codex + glm) — 2 blockers fixed + re-verified
codex/glm found: (1) stop→restart interleave, (2) gap≠non-overlap, (glm) mid-run
`prog` mutation wedges `running`/skips `/stop`. Fixed: run-generation token +
ownership-guarded `finally`, `prog.slice()` snapshot, `max(gap,motionMs,MIN_STEP_MS)`
wait, failed-step abort. codex re-verify: **JS run-loop races RESOLVED**. One narrow
residual it raised — an already-in-flight `fetch` from an old run can reach the
controller just after Stop — is mitigated by an `AbortController` (cancels in-flight
on Stop) and bounded by the server drive-watchdog (any leaked pulse auto-stops in
≤500 ms) + E-STOP. The FULL fix is a controller-side session/abort (a Go change) —
noted as a follow-up.

## Post-execution report

**Implemented** (front-end only, no Go handler change): Enter→send (form), clickable
Commands menu (`pick`), and a program builder — `addStep`/`mv`/`rm`/`clearProg`, Run
with repeat+gap, Stop, and save/load named programs in localStorage. The run engine
has the run-gen token, ownership-guarded `finally`, program snapshot, motion-duration
wait, failed-step abort, MIN_STEP_MS floor, and AbortController on Stop.

**Process note:** the plan→plan-review gate was initially skipped for this feature
(coded first, then reviewed); corrected mid-stream — plan written, 2-way plan review
run, then the corrected code + code-review re-verify. Following the guidelines from
here.

**Residual (documented):** a rare Stop-timing in-flight command can cause ≤500 ms of
motion before the watchdog stops it; full fix = a controller session-abort endpoint
(follow-up). **Deploy:** rebuild arm64, restart, verify `GET /` — no motion issued by
me. ci-local PASS (104 py + Go web-UI tests).
