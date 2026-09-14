"""One-command entry: brief -> validated campaign -> human gate -> publish.

Usage:
  python run.py --brief-file demo_brief.txt --auto-approve   # demo / video
  python run.py --brief "Launch ..."                        # interactive gate
  python run.py --brief-file demo_brief.txt --no-llm        # no Ollama needed
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from prodigal_social.bus import MessageBus
from prodigal_social.llm import OllamaClient
from prodigal_social.orchestration import CampaignRunner, format_trace
from prodigal_social.platform import MockPlatform
Path("logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
                    handlers=[logging.FileHandler("logs/prodigal.log", encoding="utf-8"),
                              logging.StreamHandler(sys.stderr)])

REQUIRED_POST_KEYS = ["post_id", "channel", "day", "slot", "copy", "hashtags",
                      "format", "cta", "creative_brief", "compliance_status"]


def validate_campaign(c: dict) -> list[str]:
    """Code-level schema check. Returns error list (empty = valid)."""
    errs = []
    for k in ("campaign_id", "brief", "strategy", "calendar"):
        if k not in c:
            errs.append(f"missing:{k}")
    s = c.get("strategy", {})
    for k in ("objective", "audience", "channel_mix", "pillars"):
        if k not in s:
            errs.append(f"strategy.missing:{k}")
    try:
        if abs(sum(float(v) for v in s.get("channel_mix", {}).values()) - 1.0) > 0.05:
            errs.append("strategy.channel_mix must sum to ~1")
    except Exception:
        errs.append("strategy.channel_mix malformed")
    for i, p in enumerate(c.get("calendar", [])):
        for k in REQUIRED_POST_KEYS:
            if k not in p:
                errs.append(f"post[{i}].missing:{k}")
        if p.get("channel") not in ("buzz", "forum", "pro"):
            errs.append(f"post[{i}].bad_channel")
    if not c.get("calendar"):
        errs.append("calendar empty")
    return errs


def review_loop(runner: CampaignRunner, brief: str, args) -> dict:
    """Build -> validate -> human gate. Rejected => re-run strategy/writer."""
    parsed = runner.agents["orchestrator"].parse_brief(brief)
    strategy = runner.agents["strategy"].plan(parsed)
    for round_no in range(1, 4):  # max 3 human review rounds, then stop
        week1 = runner.build_week(f"cmp-cli-{round_no}", strategy, week=1)
        campaign = {"campaign_id": f"cmp-cli-{round_no}", "brief": brief,
                    "strategy": strategy, "calendar": week1}
        errs = validate_campaign(campaign)
        if errs:
            print(f"[validate] round {round_no} errors: {errs} -> rebuilding")
            strategy = runner.agents["strategy"].plan(parsed)
            continue
        print(f"\n===== CAMPAIGN {campaign['campaign_id']} "
              f"({len(week1)} posts, round {round_no}) =====")
        print(f"Objective: {strategy.get('objective')} | Audience: {strategy.get('audience')}")
        for p in week1:
            print(f"  [{p['channel']:5} d{p['day']} {p['slot']}] ({p['format']}/{p['cta']}) "
                  f"{p['copy'][:75]}... | {','.join(p['hashtags'])}")
        if args.auto_approve:
            print("[gate] --auto-approve: approved")
            campaign["human_approval"] = {"approved": True, "by": "auto", "at": "now"}
            return campaign
        try:
            ans = input("[y]approve [s]re-plan strategy [w]rewrite posts [N]quit > ").strip().lower()
        except EOFError:  # no TTY (evaluator pipes stdin): quit without publishing
            ans = "n"
        if ans == "y":
            campaign["human_approval"] = {"approved": True, "by": "cli", "at": "now"}
            runner.bus.send("human", "orchestrator", "approval",
                            {"campaign_id": campaign["campaign_id"], "approved": True})
            return campaign
        if ans == "s":
            strategy = runner.agents["strategy"].plan(parsed)
            runner.bus.send("human", "strategy", "replan_request", {"round": round_no})
        elif ans == "w":
            runner.bus.send("human", "writer", "rewrite_request", {"round": round_no})
            continue  # same strategy, fresh drafts next loop
        else:
            runner.bus.send("human", "orchestrator", "approval",
                            {"campaign_id": campaign["campaign_id"], "approved": False})
            sys.exit("Rejected by human. No posts published.")
    sys.exit("3 review rounds exhausted. No posts published.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", default="")
    ap.add_argument("--brief-file", default="demo_brief.txt")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--db", default="data/prodigal.db")
    args = ap.parse_args()

    brief = args.brief.strip()
    if not brief and Path(args.brief_file).exists():
        brief = Path(args.brief_file).read_text(encoding="utf-8").strip()
    if not brief:
        sys.exit("Provide --brief or --brief-file")

    if args.no_llm:
        llm = OllamaClient(model=args.model,
                           request_fn=lambda u, p, t: (_ for _ in ()).throw(
                               ConnectionError("--no-llm mode")))
    else:
        llm = OllamaClient(model=args.model, log_path=Path("logs/llm_usage.jsonl"))
    runner = CampaignRunner(llm, MockPlatform(db_path=args.db),
                            MessageBus(db_path=args.db, trace_path=Path("logs/trace.jsonl")))

    # 1-2. validated campaign + review/re-run loop
    campaign = review_loop(runner, brief, args)
    cid = campaign["campaign_id"]
    runner.save_campaign(cid, brief,
                         runner.agents["orchestrator"].parse_brief(brief),
                         campaign["strategy"])
    # fix post campaign_ids to the approved cid for clean analytics
    for p in campaign["calendar"]:
        p["campaign_id"] = f"{cid}-wk1"

    # 3. handoff to Scheduler (ONLY after approval)
    published = runner.agents["scheduler"].publish_all(campaign["calendar"])
    print(f"\n[publish] {len(published)} posts live.")
    community, sensitive = [], 0
    for pub in published:
        for r in runner.agents["community"].handle(pub["post_id"]):
            community.append(r)
            sensitive += int(r["escalate"])
    report = runner.agents["analytics"].analyze(f"{cid}-wk1")
    report["week"] = 1
    print(f"[week1] imp={report['kpi_actuals'].get('impressions')} "
          f"comments={report['kpi_actuals'].get('comments')} "
          f"escalated={sensitive}")
    for rec in report.get("recommendations", [])[:3]:
        print(f"  REC: {rec['action']} ({rec['expected_lift']})")

    # week 2 improvement loop
    strategy2 = runner.agents["strategy"].plan(
        {"objective": campaign["strategy"].get("objective", "")}, report.get("recommendations", []))
    week2 = runner.build_week(cid, strategy2, week=2, slots=runner._improved_slots(report))
    for p in week2:
        p["campaign_id"] = f"{cid}-wk2"
    published2 = runner.agents["scheduler"].publish_all(week2)
    report2 = runner.agents["analytics"].analyze(f"{cid}-wk2")
    w1, w2 = report["kpi_actuals"], report2["kpi_actuals"]
    print(f"[week2] imp={w2.get('impressions')} "
          f"(delta {((w2.get('impressions', 1) / max(1, w1.get('impressions', 1))) - 1) * 100:+.0f}%)")

    print("\n--- TRACE (first 25) ---")
    print(format_trace(runner.bus.all(), 25))
    print(f"\nLLM usage: {llm.usage.summary()}")
    Path("logs/last_run.json").write_text(json.dumps(
        {"campaign_id": cid, "week1": w1, "week2": w2,
         "week1_recs": report.get("recommendations", [])}, indent=2))


if __name__ == "__main__":
    main()
