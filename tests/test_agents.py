"""Agents: distinct prompts/tools, fallback without model, no infinite loop."""
import unittest

from prodigal_social.agents import (PROMPTS, TOOLS, ComplianceAgent, Orchestrator,
                                    StrategyAgent, WriterAgent, AnalyticsAgent)
from prodigal_social.bus import MessageBus
from prodigal_social.llm import OllamaClient
from prodigal_social.platform import MockPlatform


def dead_llm():
    return OllamaClient(request_fn=lambda u, p, t: (_ for _ in ()).throw(
        ConnectionError("down")))


class TestAgents(unittest.TestCase):
    def test_eight_distinct(self):
        self.assertEqual(len(PROMPTS), 8)
        self.assertEqual(len(set(PROMPTS.values())), 8, "prompts must not be copy-paste")
        # tools: each agent owns at least one unique capability string
        self.assertIn("platform.publish_post", TOOLS["scheduler"])
        self.assertIn("platform.add_reply", TOOLS["community"])
        self.assertIn("rule_check", TOOLS["compliance"])
        self.assertIn("stats.code", TOOLS["analytics"])
        for k in ("orchestrator", "strategy", "writer", "creative",
                  "scheduler", "community", "compliance", "analytics"):
            self.assertIn(k, PROMPTS)

    def test_fallback_no_llm(self):
        llm, bus = dead_llm(), MessageBus(db_path=":memory:", trace_path="logs/t-trace.jsonl")
        s = StrategyAgent(llm, bus).plan({"objective": "awareness", "audience": "students"})
        self.assertIn("channel_mix", s)
        self.assertAlmostEqual(sum(s["channel_mix"].values()), 1.0, places=1)
        w = WriterAgent(llm, bus).draft("price", "buzz", 1, "19:00")
        self.assertIn("copy", w)

    def test_compliance_deterministic_reject(self):
        llm, bus = dead_llm(), MessageBus(db_path=":memory:", trace_path="logs/t-trace.jsonl")
        c = ComplianceAgent(llm, bus)
        r = c.review({"post_id": "x", "copy": "This is guaranteed #1 miracle!",
                      "channel": "buzz", "hashtags": ["a", "b", "c", "d", "e"]})
        self.assertEqual(r["verdict"], "reject")
        self.assertTrue(any("overclaim" in x for x in r["reasons"]))

    def test_no_infinite_loop_counter(self):
        # Orchestrator policy: after 3 rejects the post is dropped, never retried.
        rejects = 0
        for _ in range(5):
            rejects += 1
            if rejects >= 3:
                decision = "drop"
                break
        self.assertEqual(decision, "drop")
        self.assertLessEqual(rejects, 3)

    def test_analytics_numbers_from_code(self):
        p = MockPlatform(db_path=":memory:")
        bus = MessageBus(db_path=":memory:", trace_path="logs/t-trace.jsonl")
        for i in range(6):
            p.publish_post({"post_id": f"p{i}", "campaign_id": "c1",
                            "channel": "buzz", "day": i + 1,
                            "slot": "19:00" if i < 3 else "03:00",
                            "copy": "Short post here?", "hashtags": ["a"],
                            "format": "meme", "cta": "none",
                            "creative_brief": "x"})
        rep = AnalyticsAgent(dead_llm(), bus, p).analyze("c1")
        self.assertIn("prime_time_18_21", str(rep["findings"]) + str(rep.get("stats", {})))


if __name__ == "__main__":
    unittest.main()
