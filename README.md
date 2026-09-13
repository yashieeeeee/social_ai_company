# Prodigal Social — Multi-Agent Social Media Company (fully local)

8 agents run a social agency on your machine. Human brief in → campaign plan →
copy + creative → compliance review → **your approval** → publish to a mock
platform → simulated engagement → community replies → weekly analytics report →
Week 2 with applied recommendations and before/after numbers.

**No hosted LLM APIs anywhere.** The only network call in the codebase is
`POST http://localhost:11434/api/generate`. Verify: `grep -r "openai\|anthropic\|api.groq\|generativelanguage" --include=*.py .` returns nothing.

## 1. Setup (5 min)

| Requirement | Value |
|---|---|
| Python | 3.10+ (tested on 3.14.1, Windows 64-bit) |
| RAM | 8 GB minimum (model is 1.9 GB) |
| Ollama | 0.34+ — https://ollama.com/download |
| Model | `qwen2.5:3b` (Q4_K_M, single model for all 8 agents) |

```powershell
git clone <your-repo-url> prodigal-social
cd prodigal-social
pip install -r requirements.txt        # requests only — nothing else

ollama pull qwen2.5:3b                # 1.9 GB, one-time download
ollama serve                          # start daemon (skip if already running)
curl.exe http://localhost:11434/api/tags   # must list qwen2.5:3b
```

**SQLite needs no setup.** `data/prodigal.db` (campaigns, posts, metrics,
comments, messages) is created automatically on first run. Delete it any time
for a clean slate — all runs are reproducible from `demo_brief.txt`.

**If the model OOMs** (we hit `cudaMalloc failed` with only ~500 MB free on an
8 GB machine): close heavy apps, then force CPU-only before retrying:

```powershell
$env:CUDA_VISIBLE_DEVICES=""
ollama kill; ollama serve
ollama run qwen2.5:3b "say hi in 5 words"   # must answer before continuing
```

No Ollama at all? Every command below accepts `--no-llm`: deterministic
template fallbacks produce a full valid run so evaluators can score the
pipeline without a model.

## 2. Demo end to end (exact commands)

```powershell
# A) Full demo, non-interactive (use for the video): brief -> gate(auto) ->
#    publish 12 posts -> community -> Week-1 report -> Week-2 (+before/after)
python run.py --brief-file demo_brief.txt --auto-approve --no-llm

# B) Same, with the real local model (after the OOM check above passes):
python run.py --brief-file demo_brief.txt --auto-approve

# C) Interactive gate — approve / re-plan strategy / rewrite posts / quit:
python run.py --brief "We are launching a budget espresso machine for students. Two-week awareness, playful, don't over-promise."

# D) Tests (mocked LLM, no model needed) + offline no-LLM proof:
python -m unittest discover -s tests          # 27 tests, must print OK
```

Expected output of (A): `[publish] 12 posts live`, `[week1] imp=~32k`,
`[week2] imp=~43k (delta +30%)`, then a 25-line agent trace and
`LLM usage: {...}`. Artifacts: `logs/trace.jsonl` (every inter-agent message),
`logs/last_run.json` (before/after numbers), `logs/prodigal.log`.

## 3. Architecture

One model (`qwen2.5:3b`), 8 agent classes (`prodigal_social/agents.py`), one
SQLite DB as shared state, one `messages` table + `logs/trace.jsonl` as the
message bus, one `CampaignRunner` (`prodigal_social/orchestration.py`) as the
only router. Agents never call each other — all traffic is `bus.send()`.

```
[human brief.txt]
       |
       v
[Orchestrator] --parse--> [Strategy] --plan--> [Orchestrator]
       |                                            |
       |--> [Writer] <---> [Creative]               |
       |         |                                  |
       |         v                                  |
       |   [Compliance] --reject(<=3x)--> [Writer]  |
       |         |  (4th failure -> human_queue,     |
       |         |   NEVER loops)                   |
       |    approve                                 |
       v         v                                  v
 >>> HUMAN APPROVAL GATE (CLI y/n; NOTHING publishes before 'y') <<<
       |
       v
[Scheduler] --publish()--> [MockPlatform: buzz/forum/pro]
       |                          | deterministic simulator
       |                          v
       |                   [metrics + comments]
       v                          v
[Community] <--read comments--> [MockPlatform] --sensitive--> [human_queue]
       |
       v
[Analytics] --pure-Python stats--> [Weekly Report]
       |
       +--(stretch)--> [Strategy.plan(recommendations)] --> Week 2 --> before/after
```

Module map: `llm.py` (localhost-only wrapper, JSON repair ×3, fallbacks,
token ledger) · `bus.py` (bus + trace) · `platform.py` (mock API + hidden
rules) · `agents.py` (8 prompts/tools) · `orchestration.py` (retry caps,
memory, gate) · `run.py` (CLI entry).

## 4. What I cut (scope log — solo, ~4 days, 8 GB CPU-only)

1. **Two-model setup (3B router + 7–8B writer) → single `qwen2.5:3b`.**
   An 8B needs ~5 GB; with 8 GB total RAM the machine OOM'd even on the 3B
   until freed. One model + per-agent temperatures (0.0 router/compliance,
   0.7 writer/creative) + code validators recovers the reliability gap.
2. **Agent framework (LangGraph/CrewAI) → ~60 lines of hand-rolled routing.**
   Own loop is fully explainable in Round 2; a framework would mean defending
   library internals instead of my design.
3. **`agents/` package with 8 files → one `agents.py` with 8 classes.**
   Multi-agent-ness comes from separate prompts + tools + bus transitions,
   not file count. Saved ~half a day of boilerplate.
4. **FastAPI server → plain function API** (`publish_post/get_feed/get_metrics/
   get_comments/add_reply`). No ports, no auth, runs offline; the brief allows it.
5. **Real image generation → 1–2 sentence creative briefs.** Explicitly permitted.
6. **Web UI → CLI + JSONL trace.** Video-friendly enough; ~half a day saved.
7. **Embeddings/RAG/memory search → SQLite `campaigns` table + messages log.**
   History is small; full-text search would be unused machinery.

## 5. Hidden engagement rules — GROUND TRUTH (`prodigal_social/platform.py:62`)

Planted in `_apply_hidden_rules`. Agents see only resulting numbers, never
these formulas. Deterministic `sha256`-based ±10% noise (NOT `randint`), so
re-runs are identical and Week 1 vs Week 2 is comparable.

| # | Rule | Formula |
|---|---|---|
| R1 | Prime time | 18–21h `×1.8` (pro `×1.4`); pro 08–10h `×1.6`; 12–13h `×1.3`; 00–05h `×0.4`; else `×1.0` |
| R2 | Question boost | copy ending `?` → comments `×2.6` (forum `×3.2`) |
| R3 | Hashtag curve | 0 tags `×0.8` · 1–2 `×1.3` · 3 `×1.0` · 4+ `×0.55` (reach) |
| R4 | Length/slang | buzz >80w `×0.5`; forum 60–180w `×1.25` else `×0.85`; pro >120w `×0.6`; slang on pro `×0.7` |
| R5 | Novelty decay | same format 2+ prior days, same channel → `0.7^streak` |
| R6 | CTA tradeoff | `link` clicks `×1.8` but likes `×0.8`; `question` comments `×1.5` |
| R7 | Overclaim backlash | `guaranteed/#1/miracle/risk-free/...` → shares `×0.6`, −5 followers, +45% negative-comment skew |

Channels: **buzz** (short video, base 2500 imp, hates long copy) ·
**forum** (discussion, base 1200, rewards depth + questions) ·
**pro** (professional, base 900, morning peak, hates slang).

**Honest scorecard (fallback-mode run):** found R1 (+106% evening, acted on,
W2 +30%), R2 (after adding Q/stmt variance to fallbacks), R6 noted but below
rec threshold. Missed R3/R4/R7 — Compliance *rejects* the violating posts, so
they never publish and Analytics has no variance to learn from (guardrail
censors the evidence; fix = log rejected drafts as counterfactuals). Missed R5
— Week-1 plan rotates formats so no streak forms, and `_stats` has no streak
detector yet.

## 6. Submission checklist

- [ ] `pip install -r requirements.txt` + `ollama pull qwen2.5:3b`
- [ ] `python -m unittest discover -s tests` → OK (25 tests, mocked)
- [ ] `python run.py --brief-file demo_brief.txt --auto-approve` → 12 posts, W1→W2 lift
- [ ] ZIP the repo **without** weights/venv/`__pycache__`/`.db` logs:
  `git archive -o submission.zip HEAD` (repo has no binaries by construction)
- [ ] Email ZIP to `surabhi@prodigalai.com`, subject
  `Task 1 Submission — <Full Name> — <College/Org>`, plus README + write-up PDF + demo link
