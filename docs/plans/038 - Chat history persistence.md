# Plan 038 — Chat history: survives reloads (and restarts)

## Goal

User request: *"make it save the chat history when reloaded (or add recent
chats)"*. Today a page reload wipes the chat log — the transcript lives
only in the browser DOM. New: the chat service keeps a bounded display
transcript, persists it to disk on the rover, and the web page rebuilds
the log from it on load. History therefore survives page reloads AND chat
service/rover restarts. (Streaming and a stop button remain deferred —
separate, later plan.)

## Design

### Chat service (agent_chat.py `serve()`)

- **Display transcript**: `history = collections.deque(maxlen=CHAT_HIST_MAX
  = 200)` of `{"who": "you"|"bot", "text": str, "ts": epoch-seconds}`.
  The serve worker records the user text at turn start and the bot entry
  as the FULL turn reply (`session.handle`'s joined output — exactly the
  single bot bubble the page shows today; Opus wording fix). Empty user
  text or empty reply entries are skipped (a direct `POST /chat` with
  `text:""` must not record a blank pair).
- **Persistence**: JSONL append to a path resolved as `serve(...,
  hist_path=None)` param → `ROVER_CHAT_HIST` env → default
  `~/rover-chat-history.jsonl`. **Test isolation is mandatory** (codex
  catch — existing ServeTests would otherwise write the developer's real
  home file): every serve test passes an explicit temp `hist_path`; the
  existing `ServeTest.setUp` is updated accordingly. On serve start, load
  the last `CHAT_HIST_MAX` lines (tolerant: corrupt/partial lines
  skipped). Writes are best-effort append-per-entry (a failed write logs
  and never breaks a turn); the file is pruned to the last 1000 lines at
  startup.
- **Locking** (both reviewers — ThreadingHTTPServer handler threads CAN
  hit `RuntimeError: deque mutated during iteration` against the worker):
  history appends and snapshots go under the serve loop's existing
  `slock`, and the DISK append happens OUTSIDE the lock (holding it
  across IO would stall /chat_status and /chat_poll — Opus). File pruning
  rewrites via temp + `os.replace` (a crash mid-rewrite must not lose
  history); the path/env is resolved at `serve()` time, never at module
  import.
- **In-flight turns, accepted**: the user entry is recorded at turn
  start, so a reload mid-turn shows a user bubble with no reply yet —
  explicitly accepted for a display transcript (the reply appears in the
  live log when polling completes; history catches it on the next
  reload).
- **Endpoint**: `GET /chat_history` → `{"history": [...]}` (oldest
  first) — SAME path on the service and the controller (Opus parity note:
  every other chat route matches 1:1; `/history` would break
  `_chat_proxy` greppability). Served from the same tiny HTTP loop as
  /chat_status et al.

### Controller (rovercontrold.py)

- `GET /chat_history` → `_chat_proxy("GET", "/chat_history")`; service down →
  `{"history": []}` with 200 (an empty log is the honest degraded answer,
  same philosophy as /chat_status's `{"up": false}`). Upstream HTTP errors
  keep `_chat_proxy`'s existing passthrough behavior.

### Web page (rovercontrold_page.py)

- `loadChatHistory(force)` (both reviewers, Opus sharpest — on a rover the
  service is usually DOWN at page load, so the initial fetch legitimately
  returns `[]`; that empty answer must never burn the flag or the
  post-chatStart fetch would be suppressed and "survives restart" would
  fail in its headline scenario): fetch `/chat_history`;
  `chatHistLoaded` is set ONLY after a non-empty rebuild actually
  renders. Rendering: remove the default greeting line and PREPEND the
  history bubbles + a `— earlier messages —` divider ABOVE any existing
  live bubbles (never `innerHTML=''` — live messages and the "chatbot
  started" sys line survive; Opus). `chatStart` success calls
  `loadChatHistory(true)`, where force first removes any previously
  rendered history block then re-renders — replacement, never
  duplication. Polling untouched.

## Deliverables

- agent_chat.py: history deque + JSONL persistence + `/chat_history`.
- rovercontrold.py: `/chat_history` proxy.
- rovercontrold_page.py: rebuild-on-load.
- docs/reference/controller-commands.md: row.
- Tests: serve-side — turns recorded (user + full-reply bot entry, blank
  pairs skipped), bound respected, oldest-first `/chat_history` shape,
  JSONL roundtrip (write → NEW serve instance → loaded), corrupt-line
  tolerance, explicit hist_path isolation (ALL serve-spawning tests incl.
  the existing ServeTest pass a temp path — no test may touch ~), env
  resolution order pin; controller — FakeChatUpstream gains
  `/chat_history`, passthrough asserted, down → `(200, {"history": []})`,
  route added to the smoke list; page markers — `/chat_history` fetch,
  the non-empty-only `chatHistLoaded` guard, `loadChatHistory(true)` in
  chatStart, divider text. (Markers can't catch the flag-semantics bug —
  which is why B1 is fixed in the design, per Opus.)

## Risks

Tiny; no motion. The locking decision is DEFINITIVE (see Design): handler
threads and the worker share `history`, deque iteration during append CAN
raise, so append/snapshot go under the existing `slock` with disk IO
outside it. File writes best-effort.

## Stages

1. Serve-side history + persistence + endpoint + tests.
2. Controller proxy + tests.
3. Page rebuild + markers.
4. Docs, CI, review gate, PR.

## Reviews

### Plan review

- **codex** — round 1 BLOCKED ×2: page rebuild idempotency contradiction;
  serve tests writing the developer's real home file. Non-blocking:
  slock discipline, in-flight-turn acceptance, proxy HTTPError
  passthrough, FakeChatUpstream/marker specifics. **All adopted.**
- **Opus** — round 1 BLOCKED on the same two, sharper: (B1) the service is
  usually DOWN at page load, so an empty fetch must never burn the
  once-flag or the post-chatStart fetch — the headline restart scenario —
  is suppressed; (B2) same $HOME pollution with order-dependence detail.
  Non-blocking adopted: slock definitively (deque iteration CAN throw),
  full-reply wording fix, /chat_history parity both sides, prune via
  temp+replace, env at serve() time, blank-pair skip, disk IO outside the
  lock, prepend-don't-wipe rendering, ordering/route-smoke tests, and the
  note that markers can't catch B1 (fixed in design instead).

### Code review

- **glm-5.1** — PASS, all seven judged areas hold. Non-blocking:
  orphan-bot bubble on a direct blank POST (plan-conformant, unreachable
  from the page), silent prune OSError (stated best-effort), env-test
  harness duplication.
- **codex** — PASS. One real cosmetic catch adopted: `#chathist` wasn't a
  flex container so restored user bubbles would lose their right-align —
  fixed with `display:contents`. Also noted stale history remains if a
  FORCED re-fetch returns empty after a successful render (accepted:
  showing the previous render is harmless and honest).
- **Opus** — PASS. Verified every gate demand in code; proved
  you→bot ordering can never interleave (the busy mutex serializes turns
  and the bot entry lands before done is pollable); restart-harness
  bookkeeping balanced; no XSS (textContent + normalized who); no
  corruption/loss path found.

## Post-execution report

Implemented as revised: serve-side display transcript (deque 200 under
slock, JSONL append outside the lock, temp+replace prune at 1000,
param→env→default path resolved at serve time), `/chat_history` 1:1
through the controller (down → empty-200), page rebuild with the
non-empty-only flag, prepend-above-live rendering, greeting removal,
force-replace on chatStart. Deviations: history bubbles are built
manually for prepending rather than via chatAdd (append-only) — same
classes; the codex flex fix added. Deferred: streaming + stop button
(unchanged); blank-POST orphan-bot bubble accepted.

Outcomes: plan gate codex BLOCKED→PASS + Opus BLOCKED→PASS (converged on
the same two blockers); code gate 3× PASS. CI green (479 tests).
