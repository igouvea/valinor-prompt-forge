# Status Matrix

| Feature / Path / Button | Expected User Outcome | Status | Evidence | Notes |
|---|---|---|---|---|
| Dashboard launch | User sees Forge status, progress, experiment table, and steer panel | Working | annotated-screenshots/annotated-01-launch-desktop.png | No console errors captured |
| Stop button | User can stop active loop without hidden terminal | Working | annotated-screenshots/annotated-02-stop-click.png | API remained reachable after click |
| Experiment table row | User can inspect experiment history/detail without layout corruption | Working | annotated-screenshots/annotated-03-experiment-detail-after-refresh.png | Table remained stable after row click/reload |
| Narrow viewport | User can still read and scroll dashboard on 390px width | Working | annotated-screenshots/annotated-04-narrow-viewport.png | Dense data table remains scroll-based; improvement recommended |
| Steer send | User can see message field and send affordance | Untested | screenshots/raw-01-launch-desktop.png | Sending skipped to avoid mutating live queue |
| External LM Studio generation | User can run forge experiments | Untested | runs/run-02-browser-flow.txt | Real LM Studio tested separately via CLI; not triggered during UI audit |
