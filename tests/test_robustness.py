"""Robustness: garbage JSON everywhere, no loops, dead Ollama, empty briefs."""
import builtins
import unittest

from prodigal_social.agents import ComplianceAgent
from prodigal_social.bus import MessageBus
from prodigal_social.llm import OllamaClient
from prodigal_social.orchestration import CampaignRunner
from prodigal_social.platform import MockPlatform


def dead_llm():
    return OllamaClient(request_fn=lambda u, p, t: (_ for _ in ()).throw(
        ConnectionError("down")))


def garbage_llm():
    return OllamaClient(max_retries=2,
                        request_fn=lambda u, p, t: {"response": "lol not json {{{"})


class TestRobustness(unittest.TestCase):
    def test_garbage_everywhere_still_runs(self):
        r = CampaignRunner(garbage_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl"))
        out = r.run_campaign("espresso students playful", auto_approve=True)
        self.assertIn("week1_report", out)
        self.assertIn("week2_report", out)
        self.assertTrue(all(v <= 3 for v in [
            m["payload"]["attempt"] for m in r.bus.all()
            if m["mtype"] == "compliance_attempt"] or [0]))

    def test_empty_brief_no_crash(self):
        r = CampaignRunner(dead_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl"))
        out = r.run_campaign("", auto_approve=True)
        self.assertIn("week1_report", out)

    def test_poisoned_writer_kills_one_post_not_week(self):
        llm = dead_llm()
        r = CampaignRunner(llm, MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl"))
        real_draft = r.agents["writer"].draft
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("writer exploded")
            return real_draft(*a, **k)
        r.agents["writer"].draft = boom
        posts = r.build_week("cid-x", {"pillars": ["price"]}, week=1)
        self.assertGreater(len(posts), 0, "one poisoned post must not empty the week")
        self.assertTrue(any(m["mtype"] == "post_errored" for m in r.bus.all()))

    def test_eof_input_means_no_publish(self):
        r = CampaignRunner(dead_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl"))
        real_input = builtins.input
        builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())
        try:
            ok = r.approval_gate({"campaign_id": "c", "strategy": {}, "calendar": []})
        finally:
            builtins.input = real_input
        self.assertFalse(ok)

    def test_stream_failure_silent(self):
        c = OllamaClient(request_fn=lambda u, p, t: (_ for _ in ()).throw(
            TimeoutError("slow")))
        self.assertEqual(list(c.stream("hi")), [])

    def test_compliance_never_returns_garbage_verdict(self):
        c = ComplianceAgent(garbage_llm(),
                            MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl"))
        for _ in range(5):  # repeated rejects stay bounded by caller cap
            v = c.review({"post_id": "x", "copy": "ok short post", "channel": "buzz",
                          "hashtags": ["a"]})
            self.assertIn(v["verdict"], ("approve", "reject"))

    def test_streak_detector_fires_on_synthetic_streak(self):
        from prodigal_social.agents import AnalyticsAgent
        bus = MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl")
        a = AnalyticsAgent(dead_llm(), bus, MockPlatform(db_path=":memory:"))
        rows = [{"post_id": f"s{i}", "channel": "buzz", "day": i + 1, "slot": "19:00",
                 "copy": "short post here?", "hashtags": "a", "format": "meme",
                 "cta": "none", "impressions": 3000 - i * 700, "likes": 200,
                 "n_comments": 20, "shares": 30, "saves": 10, "clicks": 50,
                 "follower_delta": 2} for i in range(4)]
        stats = a._stats(rows, "cX")
        self.assertIn("novelty_watch", stats["notes"])
        self.assertEqual(stats["streaks"]["buzz"], 4)

    def test_streak_absent_honestly_reported(self):
        from prodigal_social.agents import AnalyticsAgent
        bus = MessageBus(db_path=":memory:", trace_path="logs/t3.jsonl")
        a = AnalyticsAgent(dead_llm(), bus, MockPlatform(db_path=":memory:"))
        rows = [{"post_id": f"r{i}", "channel": "buzz", "day": i + 1, "slot": "19:00",
                 "copy": "short post here", "hashtags": "a",
                 "format": ["meme", "howto", "story"][i % 3],
                 "cta": "none", "impressions": 2000, "likes": 100,
                 "n_comments": 10, "shares": 10, "saves": 5, "clicks": 20,
                 "follower_delta": 1} for i in range(3)]
        stats = a._stats(rows, "cX")
        self.assertIn("novelty_absent", stats["notes"])


if __name__ == "__main__":
    unittest.main()
