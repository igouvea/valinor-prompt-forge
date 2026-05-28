# Coverage Baseline

| Area | Feature / Path / Process | Entry Point | Expected User Outcome | Coverage Mode | Status | Last Evidence | Notes |
|---|---|---|---|---|---|---|---|
| App | Dashboard launch | `http://127.0.0.1:7777` | Dashboard renders Forge status and history | Baseline | Working | annotated-screenshots/annotated-01-launch-desktop.png | Desktop viewport tested |
| Controls | Stop button | Top controls | Stop active loop safely | Baseline | Working | annotated-screenshots/annotated-02-stop-click.png | Safe click tested |
| Controls | Refresh/state update | Browser reload / live poll | State remains readable after refresh | Baseline | Working | annotated-screenshots/annotated-03-experiment-detail-after-refresh.png | Reload smoke tested |
| Data | Experiment table | Experiments section | History rows readable; detail interaction stable | Baseline | Working | annotated-screenshots/annotated-03-experiment-detail-after-refresh.png | Five rows visible in test state |
| Layout | Narrow viewport | 390x844 viewport | Page remains readable and scrollable | Baseline | Working | annotated-screenshots/annotated-04-narrow-viewport.png | Dense table noted |
| Input | Steer message field | Right panel | User can enter and send steering | Baseline | Untested | screenshots/raw-01-launch-desktop.png | Sending skipped to avoid mutating live queue |
| Runtime | LM Studio experiment generation | Forge CLI/dashboard start | Agents run local model workflows | Baseline | Untested | runs/run-03-final-validation.txt | Tested via CLI outside visual audit; not triggered from UI |
