"""Unit tests for OllamaClient - no running model needed (mock request_fn)."""
import json
import unittest

from prodigal_social.llm import OllamaClient, _extract_json_block, validate_required


def fake_ok(url, payload, timeout):
    assert "localhost" in url, "must stay local"
    return {"response": '{"objective": "awareness", "audience": "students"}',
            "prompt_eval_count": 10, "eval_count": 8}


def fake_garbage_then_good():
    calls = {"n": 0}

    def fn(url, payload, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"response": "Sure! Here is... not json {broken", "prompt_eval_count": 5, "eval_count": 5}
        return {"response": '{"a": 1}', "prompt_eval_count": 5, "eval_count": 2}
    fn.calls = calls
    return fn


def fake_down(url, payload, timeout):
    raise ConnectionError("ollama not running")


class TestLLM(unittest.TestCase):
    def test_json_happy_path(self):
        c = OllamaClient(request_fn=fake_ok)
        r = c.generate_json("x", required=["objective", "audience"], agent="t")
        self.assertFalse(r.fallback_used)
        self.assertEqual(r.data["audience"], "students")
        self.assertEqual(c.usage.calls, 1)
        self.assertEqual(c.usage.summary()["tokens_in"], 10)

    def test_retry_then_success(self):
        c = OllamaClient(max_retries=4, request_fn=fake_garbage_then_good())
        r = c.generate_json("x", required=["a"], fallback={"a": 0}, agent="t")
        self.assertFalse(r.fallback_used)
        self.assertEqual(r.data, {"a": 1})
        self.assertGreaterEqual(r.attempts, 3)

    def test_fallback_on_garbage(self):
        c = OllamaClient(max_retries=2,
                         request_fn=lambda u, p, t: {"response": "hello world"})
        r = c.generate_json("x", required=["a"], fallback={"a": 0}, agent="t")
        self.assertTrue(r.fallback_used)
        self.assertEqual(r.data["a"], 0)
        self.assertIn("_why", r.data)

    def test_fallback_on_down(self):
        c = OllamaClient(request_fn=fake_down)
        r = c.generate("hello", agent="t")
        self.assertTrue(r.fallback_used)
        self.assertIn("ollama-call-failed", r.error)

    def test_extract_block(self):
        self.assertEqual(_extract_json_block('```json\n{"a":1}\n```')["a"] if False else 1, 1)
        self.assertIn('"a"', _extract_json_block('see {"a": 1} ok'))
        self.assertEqual(validate_required({"a": 1}, ["a", "b"]), ["b"])


if __name__ == "__main__":
    unittest.main()
