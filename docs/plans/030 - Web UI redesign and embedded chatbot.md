# 030 — Web UI redesign + the chatbot embedded in the website

## Goal

1. **Redesign the web UI**: the page has grown into one very tall column
   (video → pads → sliders → command box → sequencer → gallery). Reorganize
   into a modern dashboard without losing ANY existing functionality.
2. **Chatbot in the website**: a 💬 Chat panel that talks to the same
   agent_chat brain (`cb`) — plain-English driving, `$` commands, scans,
   find — from the browser.

## Design

### Chat architecture (three pieces, smallest-change each)

- **`agent_chat.py` refactor**: extract the REPL turn logic into
  `ChatSession` — `handle(text) -> str` runs one full turn ($-dispatch or
  LLM tool-loop) and RETURNS the transcript text that today goes to stdout
  (bot replies, `[tool(...)]` traces, $-command output). `main()`'s REPL
  becomes a thin wrapper (behavior identical; golden-transcript tests pin
  representative $-command, tool-loop, tool-failure, and LLM-error turns,
  plus history mutation).
- **`--serve [port]` mode — ASYNC job model** (round-1 reviewer redesign:
  the controller must never hold a thread on an LLM): loopback-only
  `ThreadingHTTPServer` on **127.0.0.1:8090**; binding the port is the
  natural single-instance mutex (a second spawn dies on EADDRINUSE).
  - `POST /chat` `{"text"}` → submits the turn to the session worker
    (turns serialized by the session lock) and returns `{"turn": N}`
    **immediately**; `409 {"busy": true}` if a turn is already running.
  - `GET /chat_poll?turn=N` → `{"done": false}` or
    `{"done": true, "reply": …}` (last few turns kept).
  - `GET /chat_status` → `{"ok", "model", "rover", "dobot", "busy"}`.
  - The serve mode calls `load_dotenv()` itself (plan-029 lesson).
- **Controller bridge** (rovercontrold.py): thin pass-through proxies for
  `/chat`, `/chat_poll`, `/chat_status` — every proxied call uses a **5 s
  timeout** (submit and poll are instant server-side); down → `/chat_status`
  maps to `{"up": false}`, others 503. **No controller thread ever blocks
  on a turn** — MJPEG streams and drive endpoints keep their existing
  latency characteristics; the chat service's callbacks into the controller
  (motion/photos) are ordinary short requests with no thread ping-pong.
  `POST /chat_start`: under a controller-side lock, probe status then spawn
  the chat service detached (`setsid`, own group, `sys.executable`,
  cwd = repo dir, env inherited — the child re-loads `.env` itself; stdout/
  stderr → `~/rover-chat.log`); 409 if already up; double-click races are
  additionally killed by the EADDRINUSE mutex (the loser exits, logged).
  The controller never imports LLM code — process isolation as with cv2.
- **Timeout semantics**: there is no proxy-side turn timeout to hit — a
  long turn just keeps `busy: true`; the page shows "thinking…" for as long
  as the turn actually runs, and a controller restart mid-turn loses
  nothing (the chat service is independent; the page re-polls).
- **Page chat panel**: message list + input; send → submit + poll loop
  (1 s); input disabled while busy with a thinking indicator; `$` commands
  work verbatim; when the service is down the panel shows a **▶ start
  chatbot** button (POST /chat_start, then poll status until up).

### UI redesign (keep every id + function the tests pin)

- **Header bar**: title, health chip, the pose badge moves INTO the header
  (no more floating overlay), battery.
- **Dashboard grid** (≥ 980 px): left column — live video, camera/drive
  pads, speed, command box (+ ❔ help). Right column — one tabbed panel:
  **💬 Chat | 📷 Photos | 🌍 3D views | ⚙ Program**. The existing
  photos/scans tabs and the sequencer move into it (`showTab` extended;
  per-tab clear-all buttons unchanged). Chat is the default tab.
- **Mobile** (< 980 px): single column, tabs stay.
- Consistent card styling (rounded panels, one background/border palette);
  buttons/classes untouched. Viewers/lightboxes (3D, tour, detectors,
  photo lightbox) unchanged.
- All existing element ids, JS function names, and command aliases are
  preserved — the page-marker suite must pass with additions only.

## Deliverables

- `agent_chat.py`: `ChatSession`, `dollar_command()` extraction, `--serve`.
- `rovercontrold.py`: `/chat`, `/chat_status`, `/chat_start` (+ CHAT_PORT
  const); docs table rows.
- `rovercontrold_page.py`: dashboard layout + chat tab.
- Tests: ChatSession golden transcripts with a fake LLM client
  ($-commands work without a client; tool-loop transcript incl. a failing
  tool; LLM-error turn returns text not crash; history trim + mutation);
  serve-mode HTTP on port 0 (submit→poll roundtrip, busy 409 during a slow
  turn, poll of an unknown turn, status shape, bad-JSON 400); controller
  proxy against a fake upstream on a random port (submit/poll/status
  pass-through, `{"up":false}` when down, 503 mapping, **proxy timeout is
  5 s pinned**, chat_start 409-when-up + double-start race under the lock,
  child-exits-immediately reporting); **responsiveness guard**: with a fake
  chat upstream that never answers polls instantly plus two live MJPEG
  stream clients, a /drive request must complete within a tight bound
  (the round-1 reviewer's starvation concern, made a test); page markers
  (tab ids, chat fn names, start-button id, header-pose-badge move).
- Docs: controller-commands.md — chat endpoints + "chat from the website"
  note.

## Testing

CI (all fakes — no LLM in tests). Live: start the chat service from the web
button, drive a conversation ("what do you see", `$status`, a photo
request), confirm the transcript renders; regression-click through every
moved panel (pads, speed, sequencer run, photos, 3D views incl. boxes,
detectors, tour); mobile-width smoke via narrow window.

## Risks

- *agent_chat refactor regressions*: the REPL is the daily driver — the
  extraction keeps `main()` byte-equivalent in behavior (same prints, same
  error handling), pinned by running the existing chat tests (test_rover_cmd
  drives rover_command; new ChatSession tests cover the loop).
- *Long LLM turns*: nothing blocks on them — submit/poll are instant; the
  page shows "thinking…" for exactly as long as the turn really runs and a
  poll failure surfaces as a chat error line, not a hang.
- *Two writers on one serial port?* No — the chat service talks to the
  CONTROLLER's HTTP API (rovercontrol backend), never the serial port; the
  controller remains the single serial owner.
- *Page size*: the redesign is CSS + moved blocks; the marker suite guards
  functionality, and the 3D/lightbox layers are untouched.

## Stages

1. agent_chat: ChatSession + dollar_command + --serve (+ tests).
2. Controller bridge endpoints (+ tests); CI.
3. Page redesign + chat tab (+ marker tests); CI.
4. Deploy; live chat + full click-through regression.
5. Review gate, PR.

## Reviews

### Plan review

**Opus, round 1 — VERDICT: PASS (with non-blocking fixes to fold in).**

Judged against the code (agent_chat.py, rovercontrold.py, rovercontrold_page.py,
tests/test_controller_http.py). No blocking findings.

1. **Proxy vs. ThreadingHTTPServer / streams — NON-BLOCKING.** The controller
   uses `ThreadingHTTPServer` with `daemon_threads=True` and **no thread pool /
   no `request_queue_size` limit** — one fresh thread per request, unbounded.
   So a 180 s blocking `/chat` does NOT starve a fixed pool: `/video_feed` (its
   own blocking thread already, forever), `/drive`, `/estop` etc. keep getting
   their own threads. There is no shared lock between the proxy handler and the
   drive/estop/watchdog paths (those go through `Movement._mu` / `Rover._mu`,
   which the proxy never touches). Real risk is only unbounded thread/socket
   growth if many `/chat` calls pile up — bounded in practice by the page
   serializing sends (box disabled until reply) and the chat service's own
   session lock. Safety-critical endpoints are unaffected. *Fold in:* cap
   concurrent in-flight `/chat` proxies (e.g. a single semaphore → 503) so a
   misbehaving external client can't spawn unbounded blocked threads; keep the
   proxy's outbound socket timeout ≤ the 180 s so a dead upstream frees the
   thread.

2. **/chat_start spawn — NON-BLOCKING, but two items to pin.**
   (a) **env/keys:** the chat service must call `llm_config.load_dotenv()` itself
   at startup (exactly plan 029's fix — the controller's systemd/nohup env has no
   LLM key). `main()` already calls it; the plan must state `--serve` calls it
   too **before** constructing the OpenAI client, or chat is silently inert in
   production. Deliverables/tests should assert this.
   (b) **zombie reaping:** `setsid`/detached means the chat service outlives the
   controller and is NOT a child the controller waits on — good, but confirm the
   spawn is `Popen(..., start_new_session=True)` and the controller does **not**
   keep the handle / never `wait()`s it (fire-and-forget), matching the existing
   `start_new_session=True` subprocess pattern. If the controller keeps a handle
   without waiting, it becomes a zombie on chat-service exit.
   (c) **race of multiple spawns:** "409 when up" via a `/chat_status` probe is
   **check-then-act (TOCTOU)** — two near-simultaneous `/chat_start` both see
   "down" and both `setsid`-spawn, giving two services fighting for
   127.0.0.1:8090. *Fold in:* guard `/chat_start` with a controller-side
   `threading.Lock` + an in-flight flag, AND rely on the chat service binding
   8090 with `SO_REUSEADDR`-off so the loser's bind fails fast (loopback only).
   Serialize the spawn; don't lean on the status probe alone.

3. **ChatSession extraction — NON-BLOCKING, real divergence risk.** The REPL
   is print-driven and interleaves stdout with side effects: `(thinking…)` via
   carriage-return (`end="\r"`) then a 14-space erase, `[tool(args)]` traces,
   `bot> …` blocks, and the `$`-dispatch prints (help, dobot, scan/record/
   detect/panotest, find, rover_command). `handle(text) -> str` must **collect**
   these into the returned transcript, not emit the terminal `\r` hacks (they'd
   render as literal control chars in the browser) — strip the thinking-spinner
   entirely for the served path. Error paths matter: the REPL catches the
   LLM/network exception per-turn and prints `chat error:` (returns to prompt) —
   `handle()` must return that as text, never raise. `KeyboardInterrupt`/`EOF`
   are REPL-loop concerns (quit) and must stay in `main()`, NOT in `handle()`.
   Keep `main()` behavior byte-identical by having it print what `handle()`
   returns (or, cleaner, share a `dollar_command()`/turn helper and keep the
   REPL's live spinner only in `main()`). `trim_history` is already a clean
   pure-ish mutator — reuse as-is at the session's user-turn boundary; the
   ChatSession holds its own `messages` list (one conversation = one lock,
   matching the plan). *Test the transcript contains tool traces + bot text and
   that an exception turn returns text.*

4. **Chat→controller→chat ping-pong — NON-BLOCKING (no deadlock), but a latent
   thread-amplifier.** Confirmed the loop exists: the chat service drives the
   rover via `RoverCtl`→`rovercontrol_client`, which POSTs to
   `192.168.1.131:8080` — the SAME controller that's holding a thread blocked in
   the `/chat` proxy. Because the server is thread-per-request (no bounded
   pool), the re-entrant call gets its OWN new thread; there is **no pool
   ping-pong deadlock**. BUT: a single `/chat` turn can call `rover_scan_
   surroundings` / `record_tour` / `find`, each of which fans out many
   controller HTTP calls AND can run for minutes — so one 180 s proxy thread can
   spawn a burst of additional controller threads and hold the upstream chat
   session lock the whole time. Combined with finding (1), this is the strongest
   reason to (a) bound concurrent `/chat` proxies and (b) make the page's chat
   send strictly serial. Also note: `rovercontrol_client._TIMEOUT` is 4 s and
   `_rovercontrol_ready` probes healthz — fine, no cross-lock. Not blocking, but
   the plan should acknowledge the re-entrancy explicitly (it currently only
   says "no two serial writers", which is true but misses the thread-amplifier).

5. **UI-redesign regression surface — NON-BLOCKING (well-guarded).** The marker
   suite in `test_page_serves_all_ui_markers` pins a large set of ids/functions
   (`cmdin`, `runCmd(`, `capNum`, `clearAll(`, `toggleHelp(`, `program`,
   `roverprog:`, `pano3d(`, `showTab(`, `tabphotos`/`tabscans`/`scangrid`,
   `posebadge`/`posetext`, `poseReset(`, `scanCancel(`, `boxesCmd(`,
   `drawBoxes(`, `scansTick(`, the shader `Ry(yaw)^T`/`Rx(pitch)^T` comments,
   etc.) plus ordering tests (`boxesCmd` before `parseCmd`; variant buttons
   inside `if(!src)`). The plan's "additions only, every id/fn preserved" is the
   right contract. **Watch-outs the plan must honor:** `showTab` currently
   toggles `gallery`/`scangrid`/`clearphotos`/`clearscans` and drives
   `tabphotos`/`tabscans` button styling — extending it to a Chat/Program tab set
   must keep those exact ids and the two existing tab values working (`gtab`
   still `'photos'`/`'scans'` for `scansTick`/`load` gating). Moving the pose
   badge "into the header" must keep `id="posebadge"`/`id="posetext"` present
   (the marker + `setInterval(poseTick,500)` assert them); don't delete the
   fixed-position node's ids even if restyled. The command box moving into a
   panel must keep the `<form onsubmit="runCmd();return false">` wrapper (Enter-
   to-send marker). *Fold in:* add the new tab/chat ids to the marker suite so
   the redesign is pinned the same way (the plan says this — good).

6. **Test completeness — NON-BLOCKING, mostly covered; add:** the plan lists
   ChatSession fake-LLM tests, serve-mode HTTP on port 0, controller-proxy
   against a fake upstream, and page markers — solid and matches the existing
   fake-driven style (`RRecLink`, `identify_builder=None`, port-0 servers).
   Missing/should-add: (a) an explicit **load_dotenv-is-called** assertion for
   `--serve`; (b) a **/chat_start double-call → one spawn / second gets 409**
   test (the TOCTOU guard); (c) proxy **timeout → chat-error mapping** (plan has
   it — keep); (d) a test that `handle()` on an error turn returns text and does
   NOT raise; (e) `$`-command path returns transcript with no LLM client
   configured (plan has it — keep). No serial/motion in any new test (all fakes),
   consistent with the "no move (cat safety)" constraint — the served chat must
   never be exercised against real hardware in CI.

Minor: Deliverables typo "endpooints" (line ~68) and "chat from the website".
Non-blocking.

**Net:** architecture is sound and minimal-change; process isolation and the
loopback-only chat service are the right calls, and the marker suite makes the
UI redesign safe. Fold findings (1) proxy concurrency cap + outbound timeout,
(2) load_dotenv-in-serve + fire-and-forget spawn + TOCTOU lock on /chat_start,
and (3) transcript-capture (no `\r` spinner, error-returns-text) into stages 1–2
before implementing. No blockers.

**codex, round 1 — VERDICT: BLOCKED** on the same architecture Opus judged
survivable: 180 s blocking proxies inside the safety-critical server are a
starvation risk it would not accept without bounded concurrency or a
non-blocking job model, plus the TOCTOU spawn race, ChatSession fidelity,
turn-keeps-running timeout semantics, and stress-test gaps.

**Resolution — the design was REWRITTEN to codex's job model** (now in
Design above), which also absorbs or supersedes Opus's folds: the chat
service owns the long-running turn (submit → `{"turn": N}` immediately;
`GET /chat_poll` for the result); every controller proxy call is a 5 s
pass-through, so no controller thread ever waits on an LLM (Opus fold 1's
semaphore becomes unnecessary — worst case is short-lived 5 s threads);
timeout semantics become "the turn keeps running, the page keeps showing
thinking" (codex's ask, verbatim); `/chat_start` gets the controller-side
lock + the EADDRINUSE natural mutex (both reviewers); `--serve` calls
`load_dotenv()` itself, spawn is fire-and-forget `start_new_session` with
no kept handle, logs to `~/rover-chat.log`; ChatSession returns transcript
text with the `\r` spinner excluded and error turns returned as text
(golden-transcript tests); the test list gained the double-start race, the
load_dotenv assertion, and a **drive-responsiveness guard** under load
(streams + pending chat + drive must stay fast). The thread-amplifier note
(a chat turn triggering minutes of scans while holding the session lock) is
acknowledged: page sends are serial, the session lock serializes turns, and
submit returns 409 `busy` while one runs.

**codex, round 2 — VERDICT: PASS.** "The rewritten job model clears my
blockers"; one leftover (the stale 180 s risk note contradicting the new
semantics) fixed in place.

### Code review

- **codex** — **BLOCKED → PASS.** Two real bugs: `/chat_start` could
  double-spawn during the child's multi-second boot window (poll() ignored,
  handle overwritten) → now 409 "starting" with an identity-pinned test;
  evicted turns polled `done:false` forever → `state["active"]` gates
  pending, everything else 404 "expired" (23-turn eviction test). Its
  `$help` formatting-drift note also fixed (dedicated live event).
- **Opus** — **BLOCKED → resolved.** Its single blocker (the early-exit test
  contradicting the new orphan-guard, timing-flaky) had already been fixed
  in the codex round — it reviewed the pre-fix diff snapshot; determinism
  proved with an 8/8 stress run. Everything else verified PASS, including
  an empirical EADDRINUSE check, REPL byte-parity, serve() lock analysis,
  proxy passthrough, and the responsiveness guard.
- **glm-5.1** — **PASS.** Its eviction note = codex's (fixed); its spacing
  counts were partially mistaken (print(" ",x) matches the original 2-space
  prefixes; $help was really drifting and is fixed); its
  upstream-404-passthrough test suggestion added.
- **Live catch of my own**: the served chat on the Pi auto-detected the RAW
  SERIAL backend — a second writer on the controller's port, exactly the
  hazard the plan had claimed away. Fixed: `ROVER_NO_SERIAL=1` skips serial
  detection; `--serve` sets it before any detection (both tested);
  live-verified the service now drives via `backend: rovercontrol`.

## Post-execution report

Implemented on `feature/web-redesign-chat`; live-validated on the rover.

- **Delivered**: the dashboard redesign (header with integrated pose badge;
  video+controls cards left; Chat | Photos | 3D views | Program tab panel
  right; single-column under 980 px) and the embedded chatbot (ChatSession
  extraction, `--serve` async job model on loopback :8090, controller
  submit/poll/status proxies at 5 s, web-button `/chat_start` with
  double-spawn guards, chat panel UI).
- **Live**: chat service started from the web endpoint; `$status` and a real
  LLM turn (tool call + reply in 5 s) through the bridge; double-start 409;
  down-state `{"up":false}`; page serves the new layout. Drive latency under
  streams+chat load is a pinned test.
- **Incidents during the live stage**: (a) the rover's first reboot since
  the Go→Python port resurrected a stale `@reboot` crontab entry launching
  the deleted Go binary (an untracked copy) — it seized :8080/serial/camera;
  evicted manually, and the crontab fix needs USER action (the permission
  layer rightly blocks me from editing boot config); (b) the
  second-serial-writer catch above.
- **Reviews**: plan gate Opus PASS + codex BLOCKED→PASS (the async job model
  is codex's design); code gate codex BLOCKED→PASS, glm PASS, Opus
  BLOCKED→resolved-pre-review. CI green (~350 tests).
- **Deferred**: killing the chat service from the web (stop button); chat
  transcript persistence across page reloads; streaming partial replies.
