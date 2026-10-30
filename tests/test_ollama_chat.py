"""Ollama client: Qwen3 thinking switched off, <think> blocks stripped, old-Ollama fallback."""
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import fakes  # noqa: F401
from intelligence import ollama_chat


class TestPayloadAndParsing(unittest.TestCase):
    def test_qwen3_gets_think_off(self):
        msgs = [{"role": "system", "content": "You are George."}, {"role": "user", "content": "status"}]
        p = ollama_chat.build_payload(msgs, "qwen3:8b", 160, 0.1)
        self.assertIs(p["think"], False)
        self.assertTrue(p["messages"][0]["content"].endswith("/no_think"))
        self.assertEqual(msgs[0]["content"], "You are George.")          # caller's list untouched
        self.assertEqual(p["format"], "json")
        self.assertEqual(p["options"]["num_predict"], 160)
        self.assertEqual(p["keep_alive"], -1)

    def test_qwen25_unchanged(self):
        p = ollama_chat.build_payload([{"role": "system", "content": "x"}], "qwen2.5:7b", 160, 0.1)
        self.assertNotIn("think", p)
        self.assertEqual(p["messages"][0]["content"], "x")

    def test_extract_json(self):
        self.assertEqual(ollama_chat.extract_json('{"action": "STATUS"}'), {"action": "STATUS"})
        self.assertEqual(ollama_chat.extract_json('<think>\nhmm, status\n</think>\n{"action": "STATUS"}'),
                         {"action": "STATUS"})
        self.assertEqual(ollama_chat.extract_json('<think></think>{"action": "FUEL"}'), {"action": "FUEL"})
        self.assertEqual(ollama_chat.extract_json('so...</think> {"action": "NONE"} done'), {"action": "NONE"})
        with self.assertRaises(ValueError):
            ollama_chat.extract_json("<think>I ran out of tokens before answer")

    def test_old_ollama_retry_without_think(self):
        sent = []

        def post(payload, timeout):
            sent.append(payload)
            if "think" in payload:
                raise urllib.error.HTTPError("u", 400, "invalid option think", {}, io.BytesIO(b""))
            return {"message": {"content": '{"action": "STATUS"}'}}
        r = ollama_chat.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                             "qwen3:8b", post=post)
        self.assertEqual(r, {"action": "STATUS"})
        self.assertEqual(len(sent), 2)
        self.assertNotIn("think", sent[1])


class FakeOllama(BaseHTTPRequestHandler):
    replies = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.replies.append(body)
        if self.path == "/api/chat":
            text = '<think>\nThe pilot wants status.\n</think>\n{"action": "STATUS", "speech": "Stand by."}'
            out = {"message": {"role": "assistant", "content": text}}
        else:
            out = {}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestOverHttp(unittest.TestCase):
    def test_end_to_end_with_think_block(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        old = ollama_chat.OLLAMA_CHAT_URL
        ollama_chat.OLLAMA_CHAT_URL = f"http://127.0.0.1:{srv.server_port}/api/chat"
        try:
            from intelligence.llm_client import chat_json
            r = chat_json("You are the crew.", [], "status", model="qwen3:8b")
            self.assertEqual(r["action"], "STATUS")
            self.assertIs(FakeOllama.replies[-1]["think"], False)
        finally:
            ollama_chat.OLLAMA_CHAT_URL = old
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
