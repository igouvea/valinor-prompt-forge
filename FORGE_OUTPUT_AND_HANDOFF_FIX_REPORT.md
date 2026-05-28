# Forge Output and Handoff Fix Report

## Findings

| Failure | Evidence | Fix |
| --- | --- | --- |
| Completed planner output was overwritten after context overflow | `exp-0081` planner first wrote task-api handoff, then hit `Context size has been exceeded`; the harness ran `planner.compact1` even though all required outputs existed, and compact repair overwrote the handoff with an unrelated compact-context spec. | Compact repair now runs only when required role outputs are missing. If context overflow happens after complete artifacts are written, the harness accepts the artifacts and proceeds. |
| Runaway side files could stall the next role | `exp-0079` produced `.valinor/handoff/summaries.md` at ~1.4 GB. The prior cap path read the whole file before truncating it. | Auxiliary handoff files are capped before and after every role run using bounded binary tail reads; no full-file read is used for runaway files. |
| Validator could mark a run PASS while held-out tests failed | `exp-0080` Validator wrote `VERDICT: PASS`, but held-out scoring was `10/21`; `exp-0082` failed `8/21`. | Held-out failures override Validator PASS in `BenchmarkResult.verdict`, so results report `fail` when scoring fails. |
| Generator failure still invoked Validator | `exp-0082` generator timed out, recovery found failing visible tests, then Validator still ran and spent more LM Studio time. | Failed generator recovery now skips Validator and returns a scored failure using held-out tests, preserving evidence without burning another model turn. |
| Agents were not always fed the benchmark source of truth | `exp-0080` implemented `req.params.id`, while task-api README defines `req` as `{ method, path, body }`. | Generator and Validator kickoff/guardrails now require reading `README.md` first and treating it as the source of truth over conflicting handoff text. |
| Experiment detail row collapsed after opening | `renderRows()` rebuilt the table every `/api/live` poll and deleted inserted detail rows. | Dashboard tracks `openExpId`, caches details, and reinserts the detail row after refresh/polling. |
| Zero-grade experiments polluted optimization | Runs with no usable test signal were still visible to proposer/dashboard score channels. | Zero-test entries are filtered/suppressed in scoring, proposer history, dashboard presentation, and ratcheting. |
| Stop control needed real UI validation | User reported Stop was not working and background processes remained. | Dashboard exposes Stop and `/api/stop`; visual audit clicked Stop and verified the UI/API remained responsive. |

## Implementation

- `forge/experiment.py`
  - Added per-run benchmark workspaces, role-scoped output recovery, planner scope cleanup, bounded auxiliary handoff compaction, context-overflow compact repair, and failed-generator short-circuit scoring.
  - Treats `README.md` as the binding contract for Generator and Validator when handoff text conflicts.
  - Ensures held-out failures override Validator PASS.
- `forge/agent_cli.py`
  - Reports opencode context-window errors from JSON event logs and keeps agents rooted in benchmark workspaces.
- `forge/scorer.py`, `forge/quality.py`, `forge/orchestrator.py`
  - Suppress zero-test optimization signal and avoid judge/adoption work for no-output runs.
- `forge/dashboard.py`
  - Preserves expanded experiment details across polling, filters stale zero-test history, and exposes Stop.
- `tests/test_forge_loop.py`
  - Covers workspace isolation, zero-test filtering, detail persistence, Stop UI presence, context overflow repair, no-rewrite-after-complete-handoff, bounded compaction, failed generator recovery, and held-out failure verdict override.

## Real LM Studio Evidence

- `exp-0080`: completed full local LM Studio run; hidden scoring `10/21`; result now correctly reports fail after verdict override fix.
- `exp-0081`: local LM Studio generated task-api implementation; manual held-out scoring on its workspace passed `21/21`. The run was interrupted during Validator, exposing the stale compact-repair overwrite bug.
- `exp-0082`: full local LM Studio run completed; generator timed out and held-out scoring failed `8/21`; result correctly reported `verdict=fail` instead of trusting Validator.

## Verification

- `python -m unittest discover -s tests -p "test_*.py"`
  - Result: `Ran 23 tests ... OK`.
- `python -m compileall forge`
  - Result: all Forge modules compile.
- Visual UX audit:
  - Folder: `Evidence/visual-ux-audit-20260528-1456/`
  - Dashboard launch, Stop click, experiment table/detail refresh, and narrow viewport all validated with raw and annotated screenshots.

## Remaining Risk

The loop now fails fast and reports evidence correctly, but small local LM Studio models can still produce bad benchmark implementations. That is expected optimization signal, not a harness pass. The key harness fixes are that valid completed artifacts are no longer overwritten, context runaway files are bounded, and failed generator handoffs no longer waste Validator turns.
