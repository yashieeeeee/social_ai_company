"""Mock social platform: SQLite storage + deterministic hidden-rule simulator.

GROUND-TRUTH FILE: reference this in the write-up as the planted rules.
Agents must ONLY call the public API (publish_post/get_feed/get_metrics/
get_comments/add_reply) and must NEVER import _apply_hidden_rules.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

CHANNELS: Dict[str, Dict[str, Any]] = {
    # buzz: short video, young, hates long copy, loves meme/poll
    "buzz": {"label": "Buzz (short video)", "base_imp": 2500,
             "like_rate": 0.09, "comment_rate": 0.012, "share_rate": 0.02,
             "save_rate": 0.015, "ctr": 0.02},
    # forum: discussion, rewards depth + questions
    "forum": {"label": "Forum (text/discussion)", "base_imp": 1200,
              "like_rate": 0.06, "comment_rate": 0.03, "share_rate": 0.012,
              "save_rate": 0.02, "ctr": 0.025},
    # pro: professional, midday/morning, hates slang + long copy
    "pro": {"label": "ProLink (professional)", "base_imp": 900,
            "like_rate": 0.05, "comment_rate": 0.012, "share_rate": 0.01,
            "save_rate": 0.025, "ctr": 0.035},
}

OVERCLAIM_PAT = re.compile(
    r"\b(guaranteed|#1|best ever|miracle|risk-free|100% free|no downside|everyone loves)\b",
    re.IGNORECASE)
SLANG_PAT = re.compile(r"\b(lol|lmao|omg|yeet|vibes|slay|fr fr|no cap)\b", re.IGNORECASE)

POS_COMMENTS = [
    "This is exactly what I needed as a student, price point looks fair.",
    "Nice breakdown, saving this for later.",
    "Tried something similar last sem, curious how this compares?",
    "Clean explanation, sharing with my roommate.",
]
NEG_COMMENTS = [
    "Sounds overhyped, is this really that cheap to run?",
    "Is this a scam? That claim seems exaggerated.",
    "Too long, lost me halfway. TL;DR?",
    "How is this different from a French press? Genuine question.",
]
SENSITIVE_COMMENTS = [
    "Your ad says guaranteed results, I want a refund policy in writing.",
    "This feels misleading, reporting if no source is shared.",
    "DM me, I had a bad side effect / bad experience, need support.",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(
  post_id TEXT PRIMARY KEY, campaign_id TEXT, channel TEXT, day INTEGER,
  slot TEXT, copy TEXT, hashtags TEXT, format TEXT, cta TEXT,
  ends_with_question INTEGER, word_count INTEGER, creative_brief TEXT,
  status TEXT DEFAULT 'published', created_at REAL);
CREATE TABLE IF NOT EXISTS metrics(
  post_id TEXT PRIMARY KEY, impressions INTEGER, likes INTEGER, comments INTEGER,
  shares INTEGER, saves INTEGER, clicks INTEGER, follower_delta INTEGER);
CREATE TABLE IF NOT EXISTS comments(
  comment_id TEXT PRIMARY KEY, post_id TEXT, author TEXT, text TEXT,
  sentiment TEXT, sensitive INTEGER DEFAULT 0, reply TEXT, created_at REAL);
"""


def _noise(key: str) -> float:
    """Deterministic +/-10% noise from sha256 (NOT random.randint)."""
    h = hashlib.sha256(key.encode()).hexdigest()
    return 0.9 + (int(h[:8], 16) % 21) / 100.0  # 0.90..1.10


def _hour(slot: str) -> int:
    try:
        return int(slot.split(":")[0]) % 24
    except Exception:
        return 12


# =====================================================================
# HIDDEN RULES (GROUND TRUTH). Agents see only resulting numbers.
# R1 prime time | R2 question boost | R3 hashtag curve | R4 length+slang
# R5 novelty decay | R6 CTA trade | R7 overclaim backlash
# =====================================================================
def _apply_hidden_rules(post: Dict[str, Any], recent_formats: List[str]) -> Dict[str, Any]:
    ch = post["channel"]
    h = _hour(post.get("slot", "12:00"))
    tags = len(post.get("hashtags") or [])
    words = int(post.get("word_count") or len(post.get("copy", "").split()))
    copy = post.get("copy", "")
    ends_q = bool(post.get("ends_with_question") or copy.strip().endswith("?"))
    fmt = post.get("format", "story")
    cta = post.get("cta", "none")

    # R1: time windows (pro peaks morning, others evening)
    if ch == "pro" and 8 <= h < 10:
        time_mult, time_why = 1.6, "pro-morning-peak"
    elif 18 <= h < 21:
        time_mult, time_why = (1.4 if ch == "pro" else 1.8), "prime-evening"
    elif 12 <= h < 14:
        time_mult, time_why = 1.3, "lunch-peak"
    elif 0 <= h < 6:
        time_mult, time_why = 0.4, "night-trough"
    else:
        time_mult, time_why = 1.0, "off-peak"

    # R3: hashtag non-linearity 0->0.8 / 1-2->1.3 / 3->1.0 / 4+->0.55
    tag_mult = 0.8 if tags == 0 else (1.3 if tags <= 2 else (1.0 if tags == 3 else 0.55))

    # R4: length + slang per channel
    if ch == "buzz":
        len_mult = 0.5 if words > 80 else 1.1
    elif ch == "forum":
        len_mult = 1.25 if 60 <= words <= 180 else 0.85
    else:
        len_mult = 0.6 if words > 120 else 1.0
    slang_hit = bool(SLANG_PAT.search(copy))
    if ch == "pro" and slang_hit:
        len_mult *= 0.7

    # R5: novelty decay — same format 2 prior days same channel
    streak = 0
    for f in reversed(recent_formats):
        if f == fmt:
            streak += 1
        else:
            break
    decay_mult = (0.7 ** streak) if streak >= 2 else 1.0

    # R2: question-ending comment boost (forum stronger)
    q_mult = (3.2 if ch == "forum" else 2.6) if ends_q else 1.0

    # R6: CTA tradeoff
    cta_like, cta_click, cta_comment = 1.0, 1.0, 1.0
    if cta == "link":
        cta_click, cta_like = 1.8, 0.8
    elif cta == "question":
        cta_comment = 1.5

    # R7: overclaim backlash
    overclaim = bool(OVERCLAIM_PAT.search(copy))
    share_mult = 0.6 if overclaim else 1.0
    neg_bias = 0.45 if overclaim else 0.0

    return {"time_mult": time_mult, "time_why": time_why, "tag_mult": tag_mult,
            "len_mult": round(len_mult, 3), "decay_mult": round(decay_mult, 3),
            "q_mult": q_mult, "cta_like": cta_like, "cta_click": cta_click,
            "cta_comment": cta_comment, "share_mult": share_mult,
            "overclaim": overclaim, "neg_bias": neg_bias, "streak": streak}


class MockPlatform:
    """Public API for agents. Simulator internals stay private."""

    def __init__(self, db_path: str | Path = "data/prodigal.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- public API --
    def publish_post(self, post: Dict[str, Any]) -> Dict[str, Any]:
        post = dict(post)
        post["word_count"] = post.get("word_count") or len(post.get("copy", "").split())
        post["ends_with_question"] = int(bool(
            post.get("ends_with_question") or post.get("copy", "").strip().endswith("?")))
        tags = post.get("hashtags") or []
        with self._conn as c:
            c.execute(
                "INSERT OR REPLACE INTO posts VALUES"
                "(:post_id,:campaign_id,:channel,:day,:slot,:copy,:hashtags,:format,"
                ":cta,:ends_with_question,:word_count,:creative_brief,'published',:ts)",
                {**post, "hashtags": ",".join(tags), "ts": time.time()})
        metrics = self._simulate(post)
        with self._conn as c:
            c.execute("INSERT OR REPLACE INTO metrics VALUES"
                      "(:post_id,:impressions,:likes,:comments,:shares,:saves,:clicks,:follower_delta)",
                      {"post_id": post["post_id"], **metrics})
        self._seed_comments(post, metrics)
        return {"post_id": post["post_id"], **metrics}

    def get_feed(self, channel: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        q = "SELECT * FROM posts" + (" WHERE channel=?" if channel else "") + " ORDER BY day, slot LIMIT ?"
        args = ([channel, limit] if channel else [limit])
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def get_metrics(self, post_id: str) -> Dict[str, Any]:
        r = self._conn.execute("SELECT * FROM metrics WHERE post_id=?", (post_id,)).fetchone()
        return dict(r) if r else {}

    def get_comments(self, post_id: str) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM comments WHERE post_id=? ORDER BY created_at", (post_id,)).fetchall()]

    def add_reply(self, comment_id: str, reply: str) -> None:
        with self._conn as c:
            c.execute("UPDATE comments SET reply=? WHERE comment_id=?", (reply, comment_id))

    def all_metrics(self, campaign_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if campaign_id:
            rows = self._conn.execute(
                "SELECT p.*, m.impressions, m.likes, m.comments as n_comments, m.shares,"
                " m.saves, m.clicks, m.follower_delta FROM posts p JOIN metrics m USING(post_id)"
                " WHERE p.campaign_id=?", (campaign_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT p.*, m.impressions, m.likes, m.comments as n_comments, m.shares,"
                " m.saves, m.clicks, m.follower_delta FROM posts p JOIN metrics m USING(post_id)").fetchall()
        return [dict(r) for r in rows]

    # -- simulator (private) --
    def _recent_formats(self, channel: str, day: int) -> List[str]:
        rows = self._conn.execute(
            "SELECT format FROM posts WHERE channel=? AND day<? ORDER BY day DESC LIMIT 3",
            (channel, day)).fetchall()
        return [r[0] for r in rows]

    def _simulate(self, post: Dict[str, Any]) -> Dict[str, int]:
        ch = post["channel"]
        base = CHANNELS[ch]
        mods = _apply_hidden_rules(post, self._recent_formats(ch, int(post.get("day", 1))))
        n = _noise(post["post_id"] + str(post.get("day")))
        imp = int(base["base_imp"] * mods["time_mult"] * mods["tag_mult"]
                  * mods["len_mult"] * mods["decay_mult"] * n)
        likes = int(imp * base["like_rate"] * mods["cta_like"] * _noise(post["post_id"] + "l"))
        comments = int(imp * base["comment_rate"] * mods["q_mult"] * mods["cta_comment"]
                       * (1.3 if mods["overclaim"] else 1.0) * _noise(post["post_id"] + "c"))
        shares = int(imp * base["share_rate"] * mods["share_mult"] * _noise(post["post_id"] + "s"))
        saves = int(imp * base["save_rate"] * _noise(post["post_id"] + "v"))
        clicks = int(imp * base["ctr"] * mods["cta_click"] * _noise(post["post_id"] + "k"))
        foll = int(likes * 0.004 + shares * 0.02 + saves * 0.008 - (5 if mods["overclaim"] else 0))
        return {"impressions": max(50, imp), "likes": max(0, likes),
                "comments": max(0, comments), "shares": max(0, shares),
                "saves": max(0, saves), "clicks": max(0, clicks),
                "follower_delta": foll}

    def _seed_comments(self, post: Dict[str, Any], metrics: Dict[str, int]) -> None:
        n = min(max(1, metrics["comments"] // 8), 5) if metrics["comments"] > 0 else 1
        mods = _apply_hidden_rules(post, [])  # sentiment bias only; counts already fixed
        neg_p = 0.25 + mods["neg_bias"]
        for i in range(n):
            h = int(hashlib.sha256((post["post_id"] + str(i)).encode()).hexdigest(), 16)
            r = (h % 100) / 100.0
            if post.get("format") == "promo" and i == n - 1:
                text, sent, sens = SENSITIVE_COMMENTS[h % len(SENSITIVE_COMMENTS)], "negative", 1
            elif r < neg_p:
                text, sent, sens = NEG_COMMENTS[h % len(NEG_COMMENTS)], "negative", 0
            else:
                text, sent, sens = POS_COMMENTS[h % len(POS_COMMENTS)], "positive", 0
            with self._conn as c:
                c.execute(
                    "INSERT OR REPLACE INTO comments VALUES(?,?,?,?,?,?,?,?)",
                    (f"{post['post_id']}-c{i}", post["post_id"], f"user{h % 900 + 100}",
                     text, sent, sens, None, time.time() + i))
