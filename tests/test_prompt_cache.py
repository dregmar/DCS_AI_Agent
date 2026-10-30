"""The system prompts never change, so Ollama can reuse them from its cache; the live state rides
in the pilot's message."""
import unittest

from tests import fakes  # noqa: F401
from huey import commands
from intelligence import llm_client, ollama_chat


class TestPromptCache(unittest.TestCase):
    def test_george_system_prompt_is_fixed(self):
        sent = []
        old = ollama_chat.chat
        ollama_chat.chat = lambda messages, model, **kw: sent.append(messages) or {"action": "NONE"}
        try:
            llm_client.CONVERSATION_HISTORY.clear()
            llm_client.query_qwen("status", "weapon GUN")
            llm_client.query_qwen("arm us up", "weapon MSL, roe FREE")
        finally:
            ollama_chat.chat = old
        self.assertEqual(sent[0][0], sent[1][0])                        # same system message
        self.assertEqual(sent[0][0]["content"], llm_client.GEORGE_SYSTEM)
        last = sent[1][-1]["content"]
        self.assertTrue(last.startswith("STATE:"))
        self.assertIn("weapon MSL, roe FREE", last)
        self.assertTrue(last.endswith("PILOT: arm us up"))

    def test_huey_system_prompt_is_fixed(self):
        self.assertIs(commands.llm_system(), commands.llm_system())
        self.assertNotIn("torque 20", commands.llm_system())
        msg = commands.llm_message("0 ft AGL, torque 20", None, "status")
        self.assertEqual(msg, "STATE: 0 ft AGL, torque 20. Checklist running: none.\nPILOT: status")

    def test_prime_matches_real_calls(self):
        sent = []
        post = lambda payload, timeout: sent.append(payload) or {"message": {"content": '{"action": "NONE"}'}}
        ollama_chat.prime(llm_client.GEORGE_SYSTEM, "qwen3:8b", post=post)
        ollama_chat.chat([{"role": "system", "content": llm_client.GEORGE_SYSTEM},
                          {"role": "user", "content": "STATE: x\nPILOT: status"}], "qwen3:8b", post=post)
        prime, real = sent
        self.assertEqual(prime["messages"][0], real["messages"][0])      # same tokens to cache
        self.assertEqual(prime["options"]["num_predict"], 1)
        self.assertEqual((prime["format"], prime.get("think")), (real["format"], real.get("think")))

    def test_ipv4_endpoint(self):
        self.assertTrue(ollama_chat.OLLAMA_CHAT_URL.startswith("http://127.0.0.1:11434"))
        self.assertTrue(llm_client.OLLAMA_NATIVE.startswith("http://127.0.0.1:11434"))


if __name__ == "__main__":
    unittest.main()
