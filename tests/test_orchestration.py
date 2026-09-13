"""Orchestration: retry cap, gate-before-publish, memory, trace."""
import unittest

from prodigal_social.bus import MessageBus
from prodigal_social.llm import OllamaClient
from prodigal_social.orchestration import CampaignRunner, format_trace
from prodigal_social.platform import MockPlatform


def dead_llm():
    return OllamaClient(request_fn=lambda u, p, t: (_ for _ in ()).throw(
        ConnectionError("down")))


class TestOrchestration(unittest.TestCase):
    def test_gate_blocks_publish(self):
        r = CampaignRunner(dead_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t2.jsonl"))
        out = r.run_campaign("Launch budget espresso machine for students. Playful.",
                             auto_approve=False, approver=lambda c: False)
        self.assertEqual(out.get("status"), "rejected_by_human")
        self.assertNotIn("week1_posts", out)  # nothing published
        self.assertTrue(any(m["mtype"] == "approval" for m in r.bus.all()))

    def test_reject_cap_and_trace(self):
        r = CampaignRunner(dead_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t2.jsonl"))
        out = r.run_campaign("Launch budget espresso machine.", auto_approve=True)
        attempts = [m for m in r.bus.all() if m["mtype"] == "compliance_attempt"]
        per_post = {}
        for m in attempts:
            per_post[m["payload"]["post_id"]] = max(
                per_post.get(m["payload"]["post_id"], 0), m["payload"]["attempt"])
        self.assertTrue(all(v <= 3 for v in per_post.values()), "retry cap violated")
        self.assertIn("week1_report", out)
        self.assertIn("week2_report", out)
        self.assertTrue(r.load_history(), "memory must persist campaign")

    def test_trace_readable(self):
        r = CampaignRunner(dead_llm(), MockPlatform(db_path=":memory:"),
                           MessageBus(db_path=":memory:", trace_path="logs/t2.jsonl"))
        r.run_campaign("Test brief.", auto_approve=True)
        t = format_trace(r.bus.all())
        self.assertIn("--[", t)
        self.assertIn("orchestrator", t)


if __name__ == "__main__":
    unittest.main()
