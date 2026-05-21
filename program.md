# program.md — research-org policy for valinor-prompt-forge

> This file is the operator's lever (Karpathy's pattern). The Python orchestrator reads it as authoritative guidance for how the proposer should reason about mutations and what "good" looks like. **Edit this file to change how the loop searches**, not the proposer code.

## The goal

Drive the **multi-metric weighted score** as high as possible across all three benchmarks. Score:

```
score = 0.5 · test_pass_rate
      + 0.2 · (1 / max(1, total_cycles))
      + 0.3 · rubric_score
```

- `test_pass_rate`: fraction of benchmark vitest tests passing (0..1)
- `total_cycles`: sum of generator↔validator rework rounds across all 3 benchmarks (lower is better)
- `rubric_score`: Opus judge's rubric average (0..1)

A perfect run is `score = 1.0`.

## What you (the proposer) can do

**Mutate the three prompt files only**:
- `prompts/champion/planner.md`
- `prompts/champion/generator.md`
- `prompts/champion/validator.md`

You mutate the whole bundle per experiment. The three prompts are coupled — the validator catches the generator's mistakes which catch the planner's ambiguity. Optimizing one in isolation breaks the chain.

## What you cannot do

- Touch any file outside `prompts/champion/`. The orchestrator, benchmarks, scorer, and judge are out of scope.
- Add new tools, change the experiment-model, or modify the scoring weights. (Those are operator choices — edit this file to change them.)
- Reference the benchmarks by name in the prompts. Prompts must stay benchmark-agnostic.

## What "good" looks like

A high-scoring prompt bundle:
1. **Plans tightly.** Planner produces a spec with crisp, single-sentence acceptance criteria. No vague "make it good" criteria.
2. **Implements completely.** Generator never ships stubs, never says "TODO", runs its own tests, fixes failures.
3. **Validates skeptically.** Validator re-runs tests, doesn't trust the build report, emits `VERDICT: PASS` only when every criterion is objectively met.
4. **Is terse where it matters.** gpt-oss-20b has a finite attention budget. Verbose prompts dilute signal. Trimming wins are real.
5. **Is structured.** Section headers, numbered steps, explicit file paths. Small models follow structure better than prose.

## Common failure modes to watch for in the journal

- **Planner produces vague acceptance criteria** → generator can't tell when it's done → cycles blow up.
- **Generator skips writing tests** → validator can't run them → FAIL with no diagnostic.
- **Validator returns VERDICT: PASS when tests fail** → reward hacking; rubric score drops.
- **Model gets confused by Valinor-specific file paths** (`.valinor/handoff/spec.md` etc.) when the benchmark has no such structure → planner writes spec to the wrong location.

If you see the same failure mode in 3+ experiments, propose a structural change to the prompt addressing it specifically.

## Mutation discipline

- **Small, targeted edits beat full rewrites.** When proposing a mutation, change one or two specific things and explain why in your rationale. The orchestrator records your rationale per experiment.
- **Don't repeat mutations that failed.** Read the last 10 entries of `state/experiments.jsonl` before proposing. If a mutation was tried and discarded, don't propose it again unless the context has materially changed.
- **Track hypotheses.** Each proposal should name an explicit hypothesis: "I believe X is causing low scores because Y. This mutation tests that by changing Z."
- **Simpler is better.** A 1-line wording change that improves score by 0.01 is a great experiment. A 200-line rewrite that improves score by 0.05 is suspect.

## Stop condition

The loop runs until:
- `champion.score ≥ 0.95` on all 3 benchmarks AND no improvement in the last 20 experiments, OR
- Operator runs `forge stop`.

## Operator notes

The experiment model (currently `lmstudio/openai/gpt-oss-20b`) and the score weights are configured in `forge/config.py`. To change either, edit that file directly. To change *what the proposer optimizes for*, edit this file.
