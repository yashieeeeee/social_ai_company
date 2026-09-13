"""Wiring: Orchestrator routes everything. Bounded retries, memory, gate, trace.

Memory choice: SAME SQLite DB (campaigns table + messages table) + trace.jsonl
mirror. Why: survives restarts (unlike in-memory dict), queryable for
Analytics/Strategy week2, zero new deps, human-readable JSONL for the demo.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

from .agents import (AnalyticsAgent, CommunityAgent, ComplianceAgent, CreativeAgent,
                     Orchestrator, SchedulerAgent, StrategyAgent, WriterAgent)
from .bus import MessageBus
from .llm import OllamaClient
from .platform import MockPlatform

MAX_REJECTS = 3  # compliance retry cap: 3 then drop to human queue

# Week-1 slot plan: deliberately MIXED (good + bad slots/tags) so Analytics
# has variance to learn from. Week-2 plan is built from recommendations.
WEEK1_SLOTS = [
    ("buzz", 1, "19:00"), ("buzz", 2, "09:00"), ("buzz", 3, "19:30"), ("buzz", 5, "03:00"),
    ("forum", 1, "12:30"), ("forum", 2, "19:00"), ("forum", 4, "09:00"), ("forum", 6, "18:30"),
    ("pro", 1, "09:00"), ("pro", 3, "19:00"), ("pro", 4, "12:30"), ("pro", 6, "08:30"),
]


class CampaignRunner:
    def __init__(self, llm: OllamaClient, platform: MockPlatform, bus: MessageBus) -> None:
        self.llm = llm
        self.platform = platform
        self.bus = bus
        self.agents = {
            "orchestrator": Orchestrator(llm, bus, platform),
            "strategy": StrategyAgent(llm, bus, platform),
            "writer": WriterAgent(llm, bus, platform),
            "creative": CreativeAgent(llm, bus, platform),
            "scheduler": SchedulerAgent(llm, bus, platform),
            "community": CommunityAgent(llm, bus, platform),
            "compliance": ComplianceAgent(llm, bus, platform),
            "analytics": AnalyticsAgent(llm, bus, platform),
        }
        conn = self.platform._conn
        conn.execute("CREATE TABLE IF NOT EXISTS campaigns(campaign_id TEXT PRIMARY KEY,"
                     " brief TEXT, parsed TEXT, strategy TEXT, created_at REAL)")
        conn.commit()

    # -- memory --
    def save_campaign(self, cid: str, brief: str, parsed: Dict, strategy: Dict) -> None:
        conn = self.platform._conn
        conn.execute("INSERT OR REPLACE INTO campaigns VALUES(?,?,?,?,?)",
                     (cid, brief, json.dumps(parsed), json.dumps(strategy), time.time()))
        conn.commit()
        self.bus.send("orchestrator", "*", "memory_saved", {"campaign_id": cid})

    def load_history(self) -> List[Dict[str, Any]]:
        conn = self.platform._conn
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    # -- build one post with bounded compliance loop --
    def build_post(self, pillar: str, channel: str, day: int, slot: str,
                   cid: str, idx: int) -> Dict[str, Any] | None:
        writer = self.agents["writer"]
        creative = self.agents["creative"]
        compliance = self.agents["compliance"]
        feedback = ""
        try:
            for attempt in range(1, MAX_REJECTS + 1):
                d = writer.draft(pillar, channel, day, slot, feedback)
                brief = creative.brief(d["copy"], d["format"], channel)
                post = {"post_id": f"{cid}-w{day}-{channel}-{idx}", "campaign_id": cid,
                        "channel": channel, "day": day, "slot": slot, "copy": d["copy"],
                        "hashtags": d.get("hashtags", []), "format": d.get("format", "story"),
                        "cta": d.get("cta", "none"),
                        "ends_with_question": d["copy"].strip().endswith("?"),
                        "word_count": len(d["copy"].split()), "creative_brief": brief,
                        "compliance_status": "pending"}
                verdict = compliance.review(post)
                self.bus.send("orchestrator", "*", "compliance_attempt",
                              {"post_id": post["post_id"], "attempt": attempt,
                               "verdict": verdict["verdict"]})
                if verdict["verdict"] == "approve":
                    post["compliance_status"] = "approved"
                    return post
                feedback = "; ".join(verdict.get("reasons", []))
        except Exception as e:  # one poisoned post must not kill the week
            self.bus.send("orchestrator", "human_queue", "post_errored",
                          {"slot": [channel, day, slot], "error": str(e)[:160]})
            return None
        # cap hit -> human queue, NEVER loop again. Snapshot the censored draft
        # so Analytics can count what Compliance kept out of the metrics.
        self.bus.send("orchestrator", "human_queue", "post_dropped",
                      {"post_id": post["post_id"], "reasons": feedback,
                       "censored": {"channel": post["channel"], "slot": post["slot"],
                                    "format": post["format"], "cta": post["cta"],
                                    "word_count": post["word_count"],
                                    "n_tags": len(post["hashtags"]),
                                    "ends_with_question": post["ends_with_question"]}})
        return None

    def build_week(self, cid: str, strategy: Dict, week: int,
                   slots: List[tuple] | None = None) -> List[Dict[str, Any]]:
        pillars = strategy.get("pillars", ["price", "dorm-life", "taste"])
        slots = slots if slots is not None else WEEK1_SLOTS
        posts, dropped = [], 0
        for i, (ch, day, slot) in enumerate(slots):
            p = self.build_post(pillars[i % len(pillars)], ch, day + (week - 1) * 7,
                                slot, f"{cid}-wk{week}", i)
            if p:
                posts.append(p)
            else:
                dropped += 1
        self.bus.send("orchestrator", "*", "week_built",
                      {"week": week, "approved": len(posts), "dropped": dropped})
        return posts

    # -- human gate: NOTHING publishes until this returns True --
    def approval_gate(self, campaign: Dict[str, Any],
                      approver: Callable[[Dict], bool] | None = None) -> bool:
        if approver is not None:
            ok = approver(campaign)
        else:
            print("\n===== CAMPAIGN FOR APPROVAL =====")
            print(f"Objective: {campaign['strategy'].get('objective')}")
            print(f"Posts: {len(campaign['calendar'])} "
                  f"({sum(1 for p in campaign['calendar'] if p['compliance_status'] == 'approved')} approved)")
            for p in campaign["calendar"][:6]:
                print(f"  [{p['channel']} d{p['day']} {p['slot']}] {p['copy'][:80]}...")
            print(f"  ... +{max(0, len(campaign['calendar']) - 6)} more")
            try:
                ans = input("Approve ALL and publish? [y/N/edit] ").strip().lower()
            except EOFError:  # piped stdin / no TTY: safe default is DO NOT publish
                ans = "n"
            ok = ans == "y"
        self.bus.send("human", "orchestrator", "approval",
                      {"campaign_id": campaign["campaign_id"], "approved": ok})
        return ok

    def run_campaign(self, brief: str, auto_approve: bool = True,
                     approver: Callable[[Dict], bool] | None = None) -> Dict[str, Any]:
        cid = "cmp-" + uuid.uuid4().hex[:6]
        parsed = self.agents["orchestrator"].parse_brief(brief)
        strategy = self.agents["strategy"].plan(parsed)
        self.save_campaign(cid, brief, parsed, strategy)
        week1 = self.build_week(cid, strategy, week=1)
        campaign = {"campaign_id": cid, "brief": brief, "strategy": strategy,
                    "calendar": week1,
                    "human_approval": {"approved": False, "by": "cli", "at": time.time()}}
        if not self.agents["orchestrator"].ready_to_run(len(week1)):
            return {"campaign_id": cid, "error": "quorum not met", "calendar": week1}
        ok = self.approval_gate(campaign, approver=(lambda c: True) if auto_approve else approver)
        campaign["human_approval"]["approved"] = ok
        if not ok:
            return {"campaign_id": cid, "status": "rejected_by_human", "calendar": week1}
        # publish AFTER gate only
        published = self.agents["scheduler"].publish_all(week1)
        community_out, sensitive = [], 0
        for pub in published:
            for r in self.agents["community"].handle(pub["post_id"]):
                community_out.append(r)
                sensitive += int(r["escalate"])
        report = self.agents["analytics"].analyze(f"{cid}-wk1")
        report["week"] = 1
        # week 2: feed recs back into strategy
        strategy2 = self.agents["strategy"].plan(parsed, report.get("recommendations", []))
        week2_slots = self._improved_slots(report)
        week2 = self.build_week(cid, strategy2, week=2, slots=week2_slots)
        published2 = self.agents["scheduler"].publish_all(week2)
        report2 = self.agents["analytics"].analyze(f"{cid}-wk2")
        report2["week"] = 2
        return {"campaign_id": cid, "strategy": strategy, "week1_posts": published,
                "community": community_out, "sensitive_escalated": sensitive,
                "week1_report": report, "week2_posts": published2,
                "week2_report": report2, "trace": self.bus.all()}

    def _improved_slots(self, report: Dict[str, Any]) -> List[tuple]:
        """Apply Analytics recs: evening slots, keep channel mix, vary formats."""
        rec_text = json.dumps(report.get("recommendations", []))
        evening = "18:" in rec_text or "19:" in rec_text or "evening" in rec_text
        slots = []
        for i, (ch, day, slot) in enumerate(WEEK1_SLOTS):
            if evening:
                slot = {"buzz": "19:00", "forum": "18:30", "pro": "08:30"}[ch]
            slots.append((ch, day, slot))
        return slots


def format_trace(msgs: List[Dict[str, Any]], limit: int = 40) -> str:
    lines = []
    for m in msgs[:limit]:
        arrow = f"{m['from_agent']:>12} --[{m['mtype']}]--> {m['to_agent']:<12}"
        extra = ""
        p = m.get("payload", {})
        if m["mtype"] == "published":
            extra = f"imp={p.get('impressions')}"
        elif m["mtype"] in ("rejected", "compliance_attempt"):
            extra = str(p.get("reasons") or p.get("verdict"))[:80]
        elif m["mtype"] == "approval":
            extra = f"approved={p.get('approved')}"
        elif m["mtype"] == "report_ready":
            extra = f"recs={p.get('recs')}"
        lines.append(f"{arrow} {extra}")
    return "\n".join(lines)
