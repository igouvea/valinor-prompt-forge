# valinor-prompt-forge

> Autoresearch loop that optimizes Valinor's planner / generator / validator system prompts against a local model. Fork of [karpathy/autoresearch](https://github.com/karpathy/autoresearch), retargeted from "tune nanochat training" to "tune the system prompts of a long-running autonomous coding harness."

## The idea

[Valinor](https://github.com/igouvea/valinor) is a long-running autonomous coding harness with three agents:
- **Planner** — researches and writes the spec + acceptance criteria
- **Generator** — implements the plan, writes tests, runs them
- **Validator** — skeptically verifies the result, emits `VERDICT: PASS|FAIL`

Each agent has a long, carefully engineered system prompt (the `BASE_ROLE_INSTRUCTIONS` in `valinor/src/runtime/agents/codexHarness.ts`). Those prompts were written by hand. They are probably not optimal — especially when driven by smaller local models.

This project applies [Karpathy's autoresearch pattern](https://github.com/karpathy/autoresearch) to the prompts themselves:

> Fix the eval. Fix the model. Mutate the prompt. Score. Keep what improves. Loop overnight.

Everything runs through an **already-authenticated agentic CLI** — `claude` by default (your subscription/OAuth, **no `ANTHROPIC_API_KEY`**), or `codex` for GPT models. Toggle one per run. The inner-loop agents run at the Top tier (Opus-class) so the prompts are tuned for the model Valinor actually deploys; the same `claude` CLI at Opus does the two outer-loop reasoning calls (propose a mutation, grade artifacts).

## How it works

```
proposer (Opus xhigh) → writes prompts/champion/{planner,generator,validator}.md
        ↓
for each benchmark:
    reset benchmark scratch dir
    spawn the authed CLI (claude/codex) for each role, tools scoped to the dir
    capture artifacts (spec.md, build-report.md, validation.md)
    run vitest → pass/fail count
    record cycle count
        ↓
judge (Opus) → grades artifacts vs rubric (per-criterion JSON)
        ↓
scorer → combined: 0.5·held_out_test_pass + 0.2·time_efficiency + 0.3·rubric
        ↓
ratchet: score > champion ? adopt : discard
        ↓
append experiments.jsonl, regen progress.md, update state/live.json (dashboard polls this)
        ↓
loop forever / until /goal
```

## Files

```
prompts/seed/                 — frozen initial prompts (Valinor baseline)
prompts/champion/             — current best (mutated by proposer)
benchmarks/                   — projects with brief + tests
  git-log-summary/            — CLI that summarizes git log output
  react-form-validation/      — TODO: small React form benchmark
  csv-to-json/                — TODO: data transformer benchmark
forge/
  orchestrator.py             — main loop
  proposer.py                 — Opus call: propose next prompt mutation
  judge.py                    — Opus call: grade artifact quality
  experiment.py               — spawn opencode, capture artifacts, run vitest
  scorer.py                   — multi-metric weighted score
  promote.py                  — forge promote: write champion to Valinor
  dashboard.py                — FastAPI dashboard
program.md                    — research-org policy (the file you edit)
state/
  experiments.jsonl           — append-only journal, one line per experiment
  live.json                   — current state (dashboard reads this)
  runs/<exp-NNNN>/            — per-experiment artifact archives
progress.md                   — human-readable summary, regenerated each cycle
```

## Quickstart

**Prerequisites:**
- Python 3.10+, [uv](https://docs.astral.sh/uv/)
- Node 20+ (benchmarks run `npm test` / vitest)
- An authed agentic CLI — `claude` (default; logged in via your subscription) or `codex`. **No API key.**
- Valinor checked out as sibling: `../valinor/`

```bash
uv sync

# 1. One-shot dry run against just git-log-summary, current champion prompts
uv run forge eval

# 2. Start the dashboard (separate terminal)
uv run forge dashboard
# → opens http://localhost:7777

# 3. Kick off the loop
uv run forge run
# Runs indefinitely. Ctrl-C to stop.

# 4. When you're ready to deploy the champion to Valinor:
uv run forge promote
```

## License

MIT. Carrying over from Karpathy's original.

## Acknowledgements

Hat tip to Andrej Karpathy — the autoresearch pattern is his. This project is an application of his idea to a different domain.
