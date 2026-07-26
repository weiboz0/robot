"""ChatSession + --serve (plan 030) — golden-transcript turns with a fake LLM
client and the async submit/poll HTTP contract. No LLM, no hardware."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import agent_chat as ac


class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeTC:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class FakeClient:
    """chat.completions.create pops canned messages."""
    def __init__(self, msgs):
        outer = self

        class Completions:
            def create(self, **kw):
                outer.last_kwargs = kw
                m = outer.msgs.pop(0)
                if isinstance(m, Exception):
                    raise m
                return type("R", (), {"choices": [type("C", (), {"message": m})()]})()

        class Chat:
            completions = Completions()
        self.msgs = list(msgs)
        self.chat = Chat()


class FakeRover:
    where, backend = "fake", "fake"

    def status(self):
        return {"backend": "fake"}

    def close(self):
        pass


def make_session(msgs=None):
    client = FakeClient(msgs) if msgs is not None else None
    s = ac.ChatSession(rover=None, arm=None, client=client, tools=[])
    return s, client


class NoSerialKnobTest(unittest.TestCase):
    def test_serve_mode_never_touches_serial_detection(self):
        # plan 030 live catch: the served chat on the Pi must never become a
        # second serial writer next to the controller. A fake rover_direct is
        # seeded (the real one needs pyserial, absent off-rover) whose probe
        # explodes — with the knob set it must never be consulted.
        import os
        import sys as _sys
        import types
        import rover_backend
        fake = types.ModuleType("rover_direct")

        def boom():
            raise AssertionError("serial detection must be skipped")
        fake.detect_port = boom
        old_mod = _sys.modules.get("rover_direct")
        _sys.modules["rover_direct"] = fake
        os.environ["ROVER_NO_SERIAL"] = "1"
        try:
            # boom() not firing IS the assertion; whatever HTTP backend the
            # host machine happens to expose (macOS AirPlay owns :5000), it
            # must never be serial
            r = rover_backend.detect_rover(host="127.0.0.1", timeout=0.2)
            if r is not None:
                self.assertNotEqual(r.backend, "serial")
        finally:
            del os.environ["ROVER_NO_SERIAL"]
            if old_mod is not None:
                _sys.modules["rover_direct"] = old_mod
            else:
                _sys.modules.pop("rover_direct", None)

    def test_serve_argv_sets_the_knob(self):
        # the --serve branch in main() must set ROVER_NO_SERIAL before any
        # detection runs — pin the marker in source (main() has side effects
        # we can't run here)
        import inspect
        src = inspect.getsource(ac.main)
        self.assertIn('os.environ["ROVER_NO_SERIAL"] = "1"', src)
        self.assertLess(src.index("ROVER_NO_SERIAL"), src.index("detect_rover"))


class ChatSessionTest(unittest.TestCase):
    def test_dollar_help_without_client(self):
        s, _ = make_session()
        out = s.handle("$help")
        self.assertIn("rover camera:", out)
        self.assertIn("website names also work", out)

    def test_dollar_no_rover(self):
        s, _ = make_session()
        self.assertEqual(s.handle("$status"), "rover not connected")

    def test_chat_off_without_client(self):
        s, _ = make_session()
        self.assertIn("chat off", s.handle("hello"))

    def test_plain_reply_golden(self):
        s, _ = make_session([FakeMsg(content="Hi there!")])
        self.assertEqual(s.handle("hello"), "Hi there!")
        self.assertEqual(s.messages[-1],
                         {"role": "assistant", "content": "Hi there!"})

    def test_tool_loop_transcript_and_history(self):
        s, client = make_session([
            FakeMsg(content=None, tool_calls=[FakeTC("t1", "rover_stop", "{}")]),
            FakeMsg(content="Stopped.")])
        # rover None → run_tool errors gracefully; the transcript still traces
        out = s.handle("stop the rover")
        self.assertIn("[rover_stop({})]", out)
        self.assertIn("Stopped.", out)
        roles = [m["role"] for m in s.messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])

    def test_invalid_tool_json_traced_and_recovered(self):
        s, _ = make_session([
            FakeMsg(tool_calls=[FakeTC("t1", "rover_stop", "{not json")]),
            FakeMsg(content="done")])
        out = s.handle("go")
        self.assertIn("[rover_stop: invalid JSON args]", out)
        tool_msg = [m for m in s.messages if m["role"] == "tool"][0]
        self.assertIn("not valid JSON", tool_msg["content"])

    def test_llm_error_returns_text_never_raises(self):
        s, _ = make_session([RuntimeError("boom gateway")])
        out = s.handle("hello")
        self.assertIn("chat error: boom gateway", out)

    def test_think_blocks_stripped(self):
        s, _ = make_session([FakeMsg(content="<think>secret</think>Visible")])
        self.assertEqual(s.handle("q"), "Visible")

    def test_empty_input_noop(self):
        s, _ = make_session()
        self.assertEqual(s.handle("   "), "")
        self.assertEqual(len(s.messages), 1)          # history untouched

    def test_live_events_exclude_spinner_from_transcript(self):
        s, _ = make_session([FakeMsg(content="ok")])
        kinds = []
        out = s.handle("hi", live=lambda k, t: kinds.append(k))
        self.assertIn("thinking", kinds)
        self.assertNotIn("\r", out)                   # no terminal control hacks


class ServeTest(unittest.TestCase):
    def setUp(self):
        # serve() blocks in serve_forever, so capture the server instance by
        # wrapping serve_forever — lets us learn the port-0 binding and stop it.
        # hist_path is ALWAYS an explicit temp file (plan 038 review demand):
        # no test may ever touch the real ~/rover-chat-history.jsonl.
        self.session, _ = make_session([FakeMsg(content="web hello")])
        self.session.client.msgs.append(FakeMsg(content="second"))
        self._histdir = tempfile.TemporaryDirectory()
        self.hist_path = os.path.join(self._histdir.name, "hist.jsonl")
        self._start_serve()

    def _start_serve(self):
        import agent_chat
        self._orig = ThreadingHTTPServer.serve_forever
        captured = {}

        def capture(srv_self, *a, **k):
            captured["srv"] = srv_self
            return self._orig(srv_self, *a, **k)
        ThreadingHTTPServer.serve_forever = capture
        self.th = threading.Thread(
            target=agent_chat.serve,
            args=(self.session, {"ok": True, "model": "fake",
                                 "rover": None, "dobot": False}, 0),
            kwargs={"hist_path": self.hist_path},
            daemon=True)
        self.th.start()
        deadline = time.time() + 5
        while "srv" not in captured and time.time() < deadline:
            time.sleep(0.02)
        self.srv = captured["srv"]
        self.port = self.srv.server_address[1]

    def tearDown(self):
        ThreadingHTTPServer.serve_forever = self._orig
        self.srv.shutdown()
        self._histdir.cleanup()

    def _req(self, method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_submit_poll_roundtrip(self):
        s, j = self._req("POST", "/chat", json.dumps({"text": "hi"}).encode())
        self.assertEqual(s, 200)
        turn = j["turn"]
        deadline = time.time() + 5
        while time.time() < deadline:
            s, j = self._req("GET", f"/chat_poll?turn={turn}")
            if j.get("done"):
                break
            time.sleep(0.05)
        self.assertEqual(j["reply"], "web hello")

    def test_status_shape(self):
        s, j = self._req("GET", "/chat_status")
        self.assertEqual(s, 200)
        for k in ("ok", "model", "rover", "dobot", "busy"):
            self.assertIn(k, j)

    def test_evicted_turn_is_404_not_forever_pending(self):
        # codex catch: results are capped at 20 — an evicted old turn must
        # report expired, never poll done:false forever
        for i in range(25):
            self.session.client.msgs.append(FakeMsg(content=f"r{i}"))
        turns = []
        for i in range(23):
            s, j = self._req("POST", "/chat", json.dumps({"text": "x"}).encode())
            if s == 200:
                turns.append(j["turn"])
                deadline = time.time() + 5
                while time.time() < deadline:
                    s2, j2 = self._req("GET", f"/chat_poll?turn={j['turn']}")
                    if j2.get("done"):
                        break
                    time.sleep(0.02)
        self.assertGreater(len(turns), 21)
        s, j = self._req("GET", f"/chat_poll?turn={turns[0]}")   # evicted
        self.assertEqual(s, 404)
        self.assertIn("expired", j["error"])

    def test_unknown_turn_404_bad_body_400(self):
        s, _ = self._req("GET", "/chat_poll?turn=999")
        self.assertEqual(s, 404)
        s, _ = self._req("GET", "/chat_poll?turn=abc")
        self.assertEqual(s, 400)
        s, _ = self._req("POST", "/chat", b"not json")
        self.assertEqual(s, 400)

    def test_busy_409_while_turn_runs(self):
        gate = threading.Event()
        orig = self.session.handle

        def slow(text, live=None):
            gate.wait(5)
            return "slow done"
        self.session.handle = slow
        try:
            s, j = self._req("POST", "/chat", json.dumps({"text": "a"}).encode())
            self.assertEqual(s, 200)
            s, j = self._req("POST", "/chat", json.dumps({"text": "b"}).encode())
            self.assertEqual(s, 409)
            self.assertTrue(j["busy"])
        finally:
            gate.set()
            self.session.handle = orig
        deadline = time.time() + 5                    # busy clears
        while time.time() < deadline:
            s, j = self._req("GET", "/chat_status")
            if not j["busy"]:
                break
            time.sleep(0.05)
        self.assertFalse(j["busy"])


class HistoryServeTest(ServeTest):
    """Plan 038: the display transcript — recorded, bounded, persisted."""

    def _turn(self, text):
        s, j = self._req("POST", "/chat",
                         json.dumps({"text": text}).encode())
        self.assertEqual(s, 200)
        n = j["turn"]
        deadline = time.time() + 5
        while time.time() < deadline:
            s, j = self._req("GET", f"/chat_poll?turn={n}")
            if j.get("done"):
                return j["reply"]
            time.sleep(0.02)
        raise AssertionError("turn never completed")

    def _restart_serve(self, replies=("again",)):
        self.srv.shutdown()
        ThreadingHTTPServer.serve_forever = self._orig
        self.session, _ = make_session([FakeMsg(content=r) for r in replies])
        self._start_serve()

    def test_turn_recorded_oldest_first_full_reply(self):
        reply = self._turn("hi there")
        s, j = self._req("GET", "/chat_history")
        self.assertEqual(s, 200)
        h = j["history"]
        self.assertEqual([e["who"] for e in h], ["you", "bot"])
        self.assertEqual(h[0]["text"], "hi there")
        self.assertEqual(h[1]["text"], reply)          # the FULL turn reply
        self.assertIn("ts", h[0])

    def test_blank_user_text_not_recorded(self):
        self._turn("")
        s, j = self._req("GET", "/chat_history")
        self.assertNotIn("you", [e["who"] for e in j["history"]])

    def test_survives_service_restart(self):
        self._turn("remember me")
        self._restart_serve()
        s, j = self._req("GET", "/chat_history")
        texts = [e["text"] for e in j["history"]]
        self.assertIn("remember me", texts)            # loaded from disk

    def test_corrupt_lines_tolerated(self):
        self._turn("good turn")
        with open(self.hist_path, "a", encoding="utf-8") as f:
            f.write('NOT JSON{\n{"who":"x","text":"badwho"}\n[1,2]\n')
        self._restart_serve()
        s, j = self._req("GET", "/chat_history")
        texts = [e["text"] for e in j["history"]]
        self.assertIn("good turn", texts)
        self.assertNotIn("badwho", texts)

    def test_env_var_resolution_when_no_param(self):
        # param → env → default; here: no param, env set → env wins
        self.srv.shutdown()
        ThreadingHTTPServer.serve_forever = self._orig
        env_path = os.path.join(self._histdir.name, "envhist.jsonl")
        os.environ["ROVER_CHAT_HIST"] = env_path
        try:
            import agent_chat
            self.session, _ = make_session([FakeMsg(content="env reply")])
            self._orig = ThreadingHTTPServer.serve_forever
            captured = {}

            def capture(srv_self, *a, **k):
                captured["srv"] = srv_self
                return self._orig(srv_self, *a, **k)
            ThreadingHTTPServer.serve_forever = capture
            self.th = threading.Thread(
                target=agent_chat.serve,
                args=(self.session, {"ok": True, "model": "fake",
                                     "rover": None, "dobot": False}, 0),
                daemon=True)                            # NO hist_path param
            self.th.start()
            deadline = time.time() + 5
            while "srv" not in captured and time.time() < deadline:
                time.sleep(0.02)
            self.srv = captured["srv"]
            self.port = self.srv.server_address[1]
            self._turn("env hello")
            self.assertTrue(os.path.exists(env_path))  # env path was used
        finally:
            os.environ.pop("ROVER_CHAT_HIST", None)


class HistFileHelpersTest(unittest.TestCase):
    def test_prune_bounds_file_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "h.jsonl")
            with open(p, "w") as f:
                for i in range(1500):
                    f.write(json.dumps({"who": "bot", "text": str(i)}) + "\n")
            ac._hist_prune(p, keep=1000)
            with open(p) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1000)
            self.assertIn('"500"', lines[0])           # oldest kept = #500
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_load_missing_file_empty(self):
        self.assertEqual(ac._hist_load("/nonexistent/x.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
