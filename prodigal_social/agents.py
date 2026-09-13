"""8 agents: separate classes, prompts, tools. Schemas match Prompt 1.

Each agent = 1 system prompt + restricted tools + template fallback.
Numbers (metrics, compliance verdicts, analytics lifts) NEVER come from the
LLM alone -- code computes, LLM verbalises. That is what makes a 3B reliable.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .bus import MessageBus
from .llm import OllamaClient
from .platform import OVERCLAIM_PAT, SLANG_PAT

# ---------------------------------------------------------------- prompts
# Each prompt is bespoke (role + input + output keys + hard constraints).
# Short on purpose: qwen2.5:3b degrades past ~600 prompt tokens.
# ----------------------------------------------------------------
PROMPTS: Dict[str, str] = {
    "orchestrator": (
        "You are the Chief of Staff, a terse router — NOT a writer. "
        "Read the human brief. Output JSON only with keys: objective, audience, "
        "tone, constraints(list), weeks(int). Never invent post copy. "
        "If the brief is vague, pick the most reasonable default and record it in constraints."
    ),
    "strategy": (
        "You are the Strategy Director. Given parsed brief JSON, output JSON only with keys: "
        "objective, audience, channel_mix(buzz/forum/pro floats summing to 1), "
        "pillars(3 strings), cadence_per_channel_per_week, kpis(list), rules_to_test(list). "
        "Keep totals to 12 posts/week. Favour the audience's channels, not 33/33/33."
    ),
    "writer": (
        "You are a social copywriter for students: playful, concrete, honest. "
        "Given pillar, channel, day, slot: output JSON only with keys: copy, hashtags(0-3), "
        "format(meme|howto|story|poll|promo), cta(question|link|none). "
        "Rules: buzz<=80 words; pro no slang and <=120 words; forum 60-150 words preferred. "
        "End with '?' only when it fits. Never use: guaranteed, #1, miracle, risk-free."
    ),
    "creative": (
        "You are an art director. Given post copy+format+channel, output JSON only with key: "
        "creative_brief (1-2 sentences: subject, setting, text overlay, NO image generation). "
        "Match the format: meme=bold caption, howto=step visual, poll=two-option graphic."
    ),
    "scheduler": (
        "You are the Scheduler, a deterministic clerk. You do NOT write copy. "
        "Given approved posts, order by day then slot and confirm slot strings HH:MM. "
        "Output JSON only with keys: schedule(list of post_id in publish order)."
    ),
    "community": (
        "You are the Community Manager: warm, short, human. Given one comment, output JSON "
        "only with keys: reply(<=40 words), escalate(bool), escalate_reason(string). "
        "Escalate=true when: refund/legal/report/misleading/scam/DM-support asks. "
        "Never promise refunds or legal outcomes."
    ),
    "compliance": (
        "You are Brand & Compliance, a strict gatekeeper. Given post copy+channel+hashtags, "
        "output JSON only with keys: verdict(approve|reject), reasons(list). "
        "Reject when: overclaim words (guaranteed,#1,miracle,risk-free), pro+slang, "
        "buzz>80 words, pro>120 words, >3 hashtags, promo+exclamation spam. Be terse."
    ),
    "analytics": (
        "You are the Analytics Lead. Given precomputed stats JSON (means, lifts, n), "
        "output JSON only with keys: top_posts, bottom_posts, findings(list of "
        "{signal, evidence, confidence}), recommendations(list of {action, expected_lift}). "
        "Every claim must cite the numbers given. Never invent numbers. Be specific: "
        "'shift buzz 09:00->18:30' not 'post better content'."
    ),
}

TOOLS: Dict[str, List[str]] = {
    "orchestrator": ["bus.send", "llm.generate_json"],
    "strategy": ["bus.send", "llm.generate_json"],
    "writer": ["bus.send", "llm.generate_json"],
    "creative": ["bus.send", "llm.generate_json"],
    "scheduler": ["bus.send", "platform.publish_post", "platform.get_metrics"],
    "community": ["bus.send", "platform.get_comments", "platform.add_reply"],
    "compliance": ["bus.send", "rule_check", "llm.generate_json"],
    "analytics": ["bus.send", "platform.all_metrics", "stats.code"],
}


class BaseAgent:
    name = "base"

    def __init__(self, llm: OllamaClient, bus: MessageBus, platform=None) -> None:
        self.llm = llm
        self.bus = bus
        self.platform = platform
        self.system = PROMPTS[self.name]
        self.tools = TOOLS[self.name]

    def _ask(self, user: str, required: List[str], fallback: Dict[str, Any],
             temp: float) -> Dict[str, Any]:
        r = self.llm.generate_json(user, system=self.system, temperature=temp,
                                   required=required, fallback=fallback, agent=self.name)
        self.bus.send(self.name, "orchestrator", "llm_call",
                      {"fallback": r.fallback_used, "attempts": r.attempts})
        return r.data or fallback


# ---------------- 1. Orchestrator ----------------
class Orchestrator(BaseAgent):
    name = "orchestrator"

    def parse_brief(self, brief: str) -> Dict[str, Any]:
        fb = {"objective": "awareness", "audience": "price-sensitive 18-25 students",
              "tone": "playful", "constraints": ["don't over-promise"], "weeks": 2,
              "_fallback": True}
        try:
            data = self._ask(f"Brief: {brief}", ["objective", "audience"], fb, 0.0)
        except Exception:
            data = fb
        data.setdefault("weeks", 2)
        self.bus.send("orchestrator", "strategy", "brief_parsed", data)
        return data

    def ready_to_run(self, n_approved: int) -> bool:
        return n_approved >= 3  # quorum: never publish an empty week


# ---------------- 2. Strategy ----------------
class StrategyAgent(BaseAgent):
    name = "strategy"

    def plan(self, parsed: Dict[str, Any], recs: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        fb = {"objective": parsed.get("objective", "awareness"),
              "audience": parsed.get("audience", "students"),
              "channel_mix": {"buzz": 0.4, "forum": 0.3, "pro": 0.3},
              "pillars": ["price", "dorm-life", "taste"],
              "cadence_per_channel_per_week": {"buzz": 4, "forum": 4, "pro": 4},
              "kpis": ["impressions", "comments", "ctr"],
              "rules_to_test": ["prime-time", "question-boost"],
              "_fallback": True}
        extra = f"\nPast recommendations to apply: {json.dumps(recs)}" if recs else ""
        data = self._ask(f"Parsed brief: {json.dumps(parsed)}{extra}",
                         ["objective", "channel_mix", "pillars"], fb, 0.2)
        # code guard: mix must sum to 1 (3B often returns 40/30/30 ints)
        try:
            mix = data.get("channel_mix", {})
            s = sum(float(v) for v in mix.values()) or 1.0
            data["channel_mix"] = {k: round(float(v) / s, 2) for k, v in mix.items()}
        except Exception:
            data["channel_mix"] = fb["channel_mix"]
        self.bus.send("strategy", "orchestrator", "plan_ready", data)
        return data


# ---------------- 3. Writer ----------------
class WriterAgent(BaseAgent):
    name = "writer"
    _n = 0

    def draft(self, pillar: str, channel: str, day: int, slot: str,
              feedback: str = "") -> Dict[str, Any]:
        WriterAgent._n += 1
        i = WriterAgent._n
        # Alternate ? / statement so question-boost stays observable in fallback mode.
        q_copy = (f"Student espresso for under a rupee a cup? {pillar} on a budget — "
                  "what matters most to you?")
        s_copy = (f"Student espresso for under a rupee a cup. {pillar} on a budget, "
                  "built for dorm life.")
        fb = {"copy": q_copy if i % 2 else s_copy,
              "hashtags": ["coffee", "students"] if channel != "pro" else ["coffee"],
              "format": ["meme", "howto", "story", "poll"][i % 4],
              "cta": "question" if i % 2 else "none", "_fallback": True}
        hint = f" Previous rejection, fix this: {feedback}." if feedback else ""
        data = self._ask(
            f"Pillar={pillar} channel={channel} day={day} slot={slot}.{hint} Write the post.",
            ["copy", "hashtags", "format", "cta"], fb, 0.7)
        if data.get("format") not in ("meme", "howto", "story", "poll", "promo"):
            data["format"] = fb["format"]
        data["hashtags"] = (data.get("hashtags") or [])[:3]
        self.bus.send("writer", "creative", "draft_ready", {"channel": channel, "day": day})
        return data


# ---------------- 4. Creative ----------------
class CreativeAgent(BaseAgent):
    name = "creative"

    def brief(self, copy: str, fmt: str, channel: str) -> str:
        fb = {"creative_brief": f"Dorm-desk photo, {fmt} style overlay, {channel} crop.",
              "_fallback": True}
        data = self._ask(f"Copy: {copy}\nFormat: {fmt}\nChannel: {channel}",
                         ["creative_brief"], fb, 0.7)
        self.bus.send("creative", "compliance", "brief_ready", {"format": fmt})
        return str(data.get("creative_brief", fb["creative_brief"]))[:300]


# ---------------- 5. Scheduler ----------------
class SchedulerAgent(BaseAgent):
    name = "scheduler"

    def order(self, posts: List[Dict[str, Any]]) -> List[str]:
        ids = [p["post_id"] for p in sorted(posts, key=lambda p: (p["day"], p["slot"]))]
        self.bus.send("scheduler", "orchestrator", "schedule_ready", {"order": ids})
        return ids

    def publish_all(self, posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for pid in self.order(posts):
            p = next(x for x in posts if x["post_id"] == pid)
            res = self.platform.publish_post(p)
            self.bus.send("scheduler", "platform", "published",
                          {"post_id": pid, "impressions": res.get("impressions", 0)})
            out.append({"post_id": pid, **res})
        return out


# ---------------- 6. Community ----------------
class CommunityAgent(BaseAgent):
    name = "community"

    def handle(self, post_id: str) -> List[Dict[str, Any]]:
        results = []
        for c in self.platform.get_comments(post_id):
            text = c["text"]
            # code tripwire first (never let LLM miss a legal risk)
            sensitive = bool(c.get("sensitive")) or bool(re.search(
                r"refund|legal|report|misleading|scam|support|side effect", text, re.IGNORECASE))
            fb = {"reply": "Thanks for flagging — passing this to the team today.",
                  "escalate": True, "escalate_reason": "tripwire",
                  "_fallback": True} if sensitive else {
                "reply": "Thanks! What would you like to see us cover next?",
                "escalate": False, "escalate_reason": "", "_fallback": True}
            data = self._ask(f"Comment: {text}\nSensitive_hint={sensitive}",
                             ["reply", "escalate"], fb, 0.3)
            escalate = bool(data.get("escalate") or sensitive)
            reply = str(data.get("reply", fb["reply"]))[:200]
            if not escalate:
                self.platform.add_reply(c["comment_id"], reply)
            self.bus.send("community", "human_queue" if escalate else "platform",
                          "comment_handled",
                          {"comment_id": c["comment_id"], "escalate": escalate})
            results.append({"comment_id": c["comment_id"], "reply": reply,
                            "escalate": escalate})
        return results


# ---------------- 7. Compliance ----------------
class ComplianceAgent(BaseAgent):
    name = "compliance"

    def rule_check(self, copy: str, channel: str, hashtags: List[str]) -> List[str]:
        reasons = []
        if OVERCLAIM_PAT.search(copy):
            reasons.append("overclaim language")
        if channel == "pro" and SLANG_PAT.search(copy):
            reasons.append("slang on pro channel")
        words = len(copy.split())
        if channel == "buzz" and words > 80:
            reasons.append(f"buzz too long ({words}w>80w)")
        if channel == "pro" and words > 120:
            reasons.append(f"pro too long ({words}w>120w)")
        if len(hashtags) > 3:
            reasons.append("too many hashtags")
        return reasons

    def review(self, post: Dict[str, Any]) -> Dict[str, Any]:
        hard = self.rule_check(post.get("copy", ""), post.get("channel", ""),
                               post.get("hashtags") or [])
        if hard:  # deterministic reject — no LLM needed, logged as such
            self.bus.send("compliance", "writer", "rejected",
                          {"post_id": post.get("post_id"), "reasons": hard, "via": "rules"})
            return {"verdict": "reject", "reasons": hard}
        fb = {"verdict": "approve", "reasons": [], "_fallback": True}
        data = self._ask(f"Review post: {json.dumps(post)[:1200]}",
                         ["verdict"], fb, 0.0)
        if data.get("verdict") not in ("approve", "reject"):
            data["verdict"] = "approve"
        self.bus.send("compliance", "writer" if data["verdict"] == "reject" else "scheduler",
                      data["verdict"], {"post_id": post.get("post_id")})
        return data


# ---------------- 8. Analytics (numbers from code, prose from LLM) ----------------
class AnalyticsAgent(BaseAgent):
    name = "analytics"

    def analyze(self, campaign_id: str, kpi_targets: Dict[str, float] | None = None) -> Dict[str, Any]:
        rows = self.platform.all_metrics(campaign_id)
        if not rows:
            return {"week": 1, "kpi_actuals": {}, "findings": [], "recommendations": []}
        stats = self._stats(rows, campaign_id)  # pure code — the actual rediscovery
        fb = {"top_posts": [], "bottom_posts": [],
              "findings": [{"signal": k, "evidence": v, "confidence": "medium"}
                           for k, v in stats["notes"].items()],
              "recommendations": [], "_fallback": True}
        verb = self._ask(f"Stats: {json.dumps(stats)[:2500]}. Verbalise findings.",
                         ["findings"], fb, 0.1)
        targets = kpi_targets or {"impressions": 18000, "comments": 240, "ctr": 0.02}
        kpi_vs = {k: {"actual": stats["kpis"].get(k), "target": targets.get(k),
                      "met": (stats["kpis"].get(k, 0) >= (targets.get(k, 0) or 0))}
                  for k in targets}
        report = {"kpi_actuals": stats["kpis"], "kpi_vs_targets": kpi_vs,
                  "top_posts": stats["top"], "bottom_posts": stats["bottom"],
                  "patterns": stats["patterns"], "sentiment": stats["sentiment"],
                  "stats": stats,
                  "findings": verb.get("findings", fb["findings"]),
                  "recommendations": stats["recommendations"]}
        self.bus.send("analytics", "strategy", "report_ready",
                      {"n_posts": len(rows), "recs": len(stats["recommendations"])})
        return report

    def render_markdown(self, report: Dict[str, Any], week: int) -> str:
        L = [f"# Weekly Report — Week {week}", ""]
        L.append("## KPI vs targets")
        for k, v in report.get("kpi_vs_targets", {}).items():
            L.append(f"- {k}: {v['actual']} vs target {v['target']} "
                     f"({'MET' if v['met'] else 'MISSED'})")
        L.append("\n## Top posts (hypothesis from data)")
        for t in report.get("top_posts", []):
            L.append(f"- {t['post_id']}: {t['why']}")
        L.append("\n## Bottom posts")
        for t in report.get("bottom_posts", []):
            L.append(f"- {t['post_id']}: {t['why']}")
        L.append("\n## Patterns")
        for k, v in report.get("patterns", {}).items():
            L.append(f"- {k}: {v}")
        L.append("\n## Comment sentiment & themes")
        L.append(f"- {json.dumps(report.get('sentiment', {}))}")
        L.append("\n## Recommendations (concrete, numeric)")
        for r in report.get("recommendations", []):
            L.append(f"- {r['action']} | expected {r['expected_lift']} | "
                     f"evidence {r.get('evidence_post_ids', [])[:2]}")
        return "\n".join(L)

    # -- deterministic stats (this is what finds the hidden rules) --
    def _stats(self, rows: List[Dict[str, Any]], campaign_id: str = "") -> Dict[str, Any]:
        def avg(xs):
            return sum(xs) / max(1, len(xs))

        kpis = {"impressions": sum(r["impressions"] for r in rows),
                "likes": sum(r["likes"] for r in rows),
                "comments": sum(r["n_comments"] for r in rows),
                "shares": sum(r["shares"] for r in rows),
                "clicks": sum(r["clicks"] for r in rows),
                "ctr": round(sum(r["clicks"] for r in rows) / max(1, sum(r["impressions"] for r in rows)), 4),
                "followers": sum(r["follower_delta"] for r in rows)}
        ranked = sorted(rows, key=lambda r: r["impressions"], reverse=True)

        def why(r):
            bits = [r["channel"], r["slot"], r["format"], r["cta"],
                    f"{len(r['copy'].split())}w",
                    f"{len([t for t in (r.get('hashtags') or '').split(',') if t.strip()])}tags",
                    "Q?" if "?" in r["copy"] else "stmt"]
            return " ".join(bits)

        top = [{"post_id": r["post_id"], "why": why(r)} for r in ranked[:3]]
        bottom = [{"post_id": r["post_id"], "why": why(r)} for r in ranked[-3:]]

        def slot_bucket(s):
            h = int(s.split(":")[0])
            return "evening" if 18 <= h < 21 else ("midday" if 12 <= h < 14
                   else ("night" if h < 6 else "other"))

        buckets: Dict[str, List[int]] = {}
        for r in rows:
            buckets.setdefault(slot_bucket(r["slot"]), []).append(r["impressions"])
        q_yes = [r["n_comments"] for r in rows if "?" in r["copy"]]
        q_no = [r["n_comments"] for r in rows if "?" not in r["copy"]]
        tag_b: Dict[str, List[int]] = {}
        for r in rows:
            n = len([t for t in (r.get("hashtags") or "").split(",") if t.strip()])
            tag_b.setdefault("0" if n == 0 else ("1-2" if n <= 2 else ("3" if n == 3 else "4+")),
                             []).append(r["impressions"])
        fmt_b: Dict[str, List[int]] = {}
        for r in rows:
            fmt_b.setdefault(r["format"], []).append(r["impressions"])
        ch_b: Dict[str, List[int]] = {}
        for r in rows:
            ch_b.setdefault(r["channel"], []).append(r["impressions"])
        cta_c = {"question": [r["n_comments"] for r in rows if r["cta"] == "question"],
                 "other": [r["n_comments"] for r in rows if r["cta"] != "question"]}
        cta_k = {"link": [r["clicks"] / max(1, r["impressions"]) for r in rows if r["cta"] == "link"],
                 "other": [r["clicks"] / max(1, r["impressions"]) for r in rows if r["cta"] != "link"]}
        long_buzz = [r["impressions"] for r in rows
                     if r["channel"] == "buzz" and len(r["copy"].split()) > 80]
        short_buzz = [r["impressions"] for r in rows
                      if r["channel"] == "buzz" and len(r["copy"].split()) <= 80]

        # sentiment + themes from the comments table (real data, not LLM guess)
        pos = neg = sens = 0
        themes: Dict[str, int] = {}
        try:
            cur = self.platform._conn.execute(
                "SELECT c.text, c.sentiment, c.sensitive FROM comments c "
                "JOIN posts p USING(post_id) WHERE p.campaign_id=?", (campaign_id,))
            for text, sentiment, sensitive in cur.fetchall():
                if sentiment == "positive":
                    pos += 1
                else:
                    neg += 1
                sens += int(sensitive or 0)
                for kw in ("price", "refund", "scam", "long", "love", "different", "guaranteed"):
                    if kw in text.lower():
                        themes[kw] = themes.get(kw, 0) + 1
        except Exception:
            pass

        notes, recs, patterns = {}, [], {}
        patterns["by_time"] = {k: round(avg(v)) for k, v in buckets.items()}
        patterns["by_format"] = {k: round(avg(v)) for k, v in fmt_b.items()}
        patterns["by_channel"] = {k: round(avg(v)) for k, v in ch_b.items()}
        patterns["by_hashtag"] = {k: round(avg(v)) for k, v in tag_b.items()}
        patterns["question_vs_statement_comments"] = (
            round(avg(q_yes), 1), round(avg(q_no), 1)) if q_yes and q_no else "n/a"
        patterns["tone"] = "no slang variance in week-1 drafts (compliance blocks pro slang); tone effect not testable"

        if buckets.get("evening"):
            rest = [v for k, vs in buckets.items() if k != "evening" for v in vs]
            if rest and avg(buckets["evening"]) > avg(rest) * 1.2:
                lift = avg(buckets["evening"]) / max(1, avg(rest))
                notes["prime_time_18_21"] = (
                    f"evening avg {avg(buckets['evening']):.0f} vs rest {avg(rest):.0f} "
                    f"(+{(lift - 1) * 100:.0f}%, n={len(buckets['evening'])}/{len(rest)})")
                recs.append({"action": "shift buzz/forum posts from 09:00/03:00 to 18:00-21:00",
                             "expected_lift": f"+{(lift - 1) * 100:.0f}% imp",
                             "evidence_post_ids": [r["post_id"] for r in ranked[:2]]})
        if q_yes and q_no and avg(q_yes) > avg(q_no) * 1.4:
            notes["question_boost"] = (
                f"question-ending avg {avg(q_yes):.1f} comments vs {avg(q_no):.1f} "
                f"(x{avg(q_yes) / max(0.1, avg(q_no)):.1f}, n={len(q_yes)}/{len(q_no)})")
            recs.append({"action": "end 1 in 2 forum/buzz posts with a genuine question",
                         "expected_lift": f"x{avg(q_yes) / max(0.1, avg(q_no)):.1f} comments",
                         "evidence_post_ids": [r["post_id"] for r in rows if '?' in r["copy"]][:2]})
        if tag_b.get("1-2") and tag_b.get("4+"):
            if avg(tag_b["1-2"]) > avg(tag_b["4+"]) * 1.2:
                notes["hashtag_curve"] = (
                    f"1-2 tags avg {avg(tag_b['1-2']):.0f} imp vs 4+ {avg(tag_b['4+']):.0f} "
                    f"(+{(avg(tag_b['1-2']) / max(1, avg(tag_b['4+'])) - 1) * 100:.0f}%)")
                recs.append({"action": "cap hashtags at 2; drop the 3rd/4th hashtag",
                             "expected_lift": "recover tag penalty", "evidence_post_ids": []})
        if long_buzz and short_buzz and avg(short_buzz) > avg(long_buzz) * 1.3:
            notes["buzz_length_penalty"] = (
                f"buzz <=80w avg {avg(short_buzz):.0f} vs >80w {avg(long_buzz):.0f} "
                f"(n={len(short_buzz)}/{len(long_buzz)})")
            recs.append({"action": "cut buzz copy to under 80 words",
                         "expected_lift": f"+{(avg(short_buzz) / max(1, avg(long_buzz)) - 1) * 100:.0f}% imp on buzz",
                         "evidence_post_ids": [r["post_id"] for r in ranked[-2:]]})
        if cta_c["question"] and cta_c["other"] and avg(cta_c["question"]) > avg(cta_c["other"]) * 1.3:
            notes["cta_question"] = (
                f"question-CTA avg {avg(cta_c['question']):.1f} comments vs "
                f"{avg(cta_c['other']):.1f} (n={len(cta_c['question'])}/{len(cta_c['other'])})")
        if neg > pos:
            notes["sentiment_negative"] = f"{neg} negative vs {pos} positive comments ({sens} sensitive)"
        return {"kpis": kpis, "top": top, "bottom": bottom, "notes": notes,
                "patterns": patterns,
                "sentiment": {"positive": pos, "negative": neg, "sensitive": sens, "themes": themes},
                "recommendations": recs}


def compare_weeks(w1: Dict[str, Any], w2: Dict[str, Any]) -> str:
    a, b = w1.get("kpi_actuals", {}), w2.get("kpi_actuals", {})
    lines = ["# Before/After — Week 1 vs Week 2", ""]
    for k in ("impressions", "likes", "comments", "shares", "clicks", "ctr", "followers"):
        va, vb = a.get(k, 0), b.get(k, 0)
        base = va if isinstance(va, (int, float)) and va != 0 else 1
        d = ((vb / base) - 1) * 100 if isinstance(va, (int, float)) else 0
        lines.append(f"- {k}: {va} -> {vb} ({d:+.0f}%)")
    return "\n".join(lines)
