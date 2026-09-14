# Prodigal Social — Multi-Agent Social Media Company (fully local)

8 agents run a social agency on your machine: brief in → campaign plan →
copy + creative → compliance review → your approval → publish to a mock
platform → simulated engagement → community replies → weekly report → Week 2
with before/after numbers.

No hosted LLM APIs anywhere. The only network call in the codebase is
`POST http://localhost:11434/api/generate`.

## Setup

Python 3.10+, 8 GB RAM, Ollama 0.34+ (https://ollama.com/download),
model `qwen2.5:3b` (Q4_K_M, 1.9 GB, one model for all 8 agents).

```powershell
git clone https://github.com/yashieeeeee/social_ai_company.git "prodigal social"
cd "prodigal social"
pip install -r requirements.txt        # requests only
ollama pull qwen2.5:3b
ollama serve                           # skip if already running
```

SQLite needs no setup — `data/prodigal.db` is created on first run.
Delete it any time for a clean slate.

If the model OOMs (`cudaMalloc failed`, common with <1 GB free on 8 GB machines):
close heavy apps, then force CPU-only and retry:

```powershell
$env:CUDA_VISIBLE_DEVICES=""
ollama kill; ollama serve
ollama run qwen2.5:3b "say hi in 5 words"   # must answer before continuing
```

No Ollama? Append `--no-llm` to any command below: deterministic fallbacks
produce a full valid run so the pipeline is scoreable without a model.

## Run

```powershell
python run.py --brief-file demo_brief.txt --auto-approve   # full demo, non-interactive
python run.py --brief "..."                                # interactive approval gate
python -m unittest discover -s tests                       # 27 tests, mocked LLM
```

Expected: `[publish] 12 posts live`, Week-1 report with numeric
recommendations, Week-2 before/after, then the agent trace.
Artifacts: `logs/trace.jsonl`, `logs/last_run.json`.

## Architecture

`CampaignRunner` (`prodigal_social/orchestration.py`) routes everything.
Agents never call each other — all handoffs are `bus.send()` into a SQLite
`messages` table mirrored to `logs/trace.jsonl`. Compliance gets 3 attempts
per post, then the post drops to a human queue (never loops). Nothing
publishes before the CLI approval gate returns yes (fails closed on EOF).
Analytics stats are pure-Python group-bys; the LLM only verbalises numbers.
Full box-and-arrow diagram: `docs/architecture.png` (also in the write-up PDF).

Modules: `llm.py` (localhost wrapper, JSON repair, fallbacks) ·
`platform.py` (mock API + hidden rules) · `agents.py` (8 prompts/tools) ·
`bus.py` (bus + trace) · `orchestration.py` (caps, memory, gate) · `run.py` (CLI).

## Scope cuts

Single 3B over 3B+8B (8 GB RAM cannot hold 8B); hand-rolled router over
LangGraph/CrewAI (defensible in interview); one `agents.py` over 8 files;
plain-function API over FastAPI; text creative briefs over image gen (permitted);
CLI over web UI; SQLite history over RAG (12 rows need no retrieval).

## Hidden rules — GROUND TRUTH (`prodigal_social/platform.py:62`)

Agents see only resulting numbers, never these formulas. Noise is deterministic
`sha256`-based ±10% (not `randint`), so re-runs are identical.

| # | Rule | Formula |
|---|---|---|
| R1 | Prime time | 18–21h x1.8 (pro x1.4); pro 08–10h x1.6; 12–13h x1.3; 00–05h x0.4 |
| R2 | Question boost | copy ending `?` → comments x2.6 (forum x3.2) |
| R3 | Hashtag curve | 0 tags x0.8 · 1–2 x1.3 · 3 x1.0 · 4+ x0.55 |
| R4 | Length/slang | buzz >80w x0.5; forum 60–180w x1.25 else x0.85; pro >120w x0.6; pro slang x0.7 |
| R5 | Novelty decay | same format 2+ prior days, same channel → 0.7^streak |
| R6 | CTA tradeoff | `link` clicks x1.8 but likes x0.8; `question` comments x1.5 |
| R7 | Overclaim backlash | `guaranteed/#1/miracle/...` → shares x0.6, −5 followers, +45% negative skew |

Channels: **buzz** (short video, base 2500, hates long copy) ·
**forum** (discussion, base 1200, rewards depth + questions) ·
**pro** (professional, base 900, morning peak, hates slang).

Scorecard: Analytics rediscovers R1 reliably. R2 needs question variance in
drafts; with the real 3B, drafts collapse to one format and zero questions, so
Week 2 can regress via R5 decay — observed live (−22%), mechanism identified.
R3/R4/R7 are structurally unobservable: Compliance rejects violating posts
pre-publish, so the evidence never reaches the metrics table; rejected drafts
are logged as counterfactuals (`post_dropped` bus messages).
