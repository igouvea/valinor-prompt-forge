# Bug Fixes

| Bug ID | Status | User Problem | Root Cause Evidence | Fix / Recommendation | Files Changed | Re-Test Evidence |
|---|---|---|---|---|---|---|
| BUG-001 | Fixed | Stop control previously did not reliably stop Forge from the UI | User report plus dashboard audit Step 02 | Dashboard exposes Stop button and `/api/stop`; audit confirms click keeps UI/API responsive | `forge/dashboard.py` | annotated-screenshots/annotated-02-stop-click.png |
| BUG-002 | Fixed | Experiment detail collapsed after refresh/polling | User report plus dashboard audit Step 03 | Detail state/cache preserved across refresh path | `forge/dashboard.py` | annotated-screenshots/annotated-03-experiment-detail-after-refresh.png |
| BUG-003 | Fixed | Zero-test experiments polluted optimization signal | Unit tests and dashboard table inspection | Zero-test entries filtered from presentable history and scorer zeroes aggregate-zero runs | `forge/quality.py`, `forge/scorer.py`, `forge/orchestrator.py`, `forge/dashboard.py` | runs/run-03-final-validation.txt |
| BUG-004 | Fixed | Context overflow could rewrite completed handoff outputs or read runaway side files into memory | LM Studio exp-0081/0082 evidence and unit regressions | Compact repair only runs for missing outputs; auxiliary handoff files are tail-capped with bounded reads | `forge/experiment.py`, `forge/agent_cli.py` | runs/run-03-final-validation.txt |
| BUG-005 | Recommended | Narrow viewport requires heavy scrolling for experiment comparison | annotated-screenshots/annotated-04-narrow-viewport.png | Add mobile-specific experiment cards or column picker | Not implemented | N/A |
