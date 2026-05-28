# Validation Plan

Expected quality bar: the Forge dashboard must open locally, render the prompt-optimization status without clipped controls, expose Start/Stop/Refresh actions, preserve experiment detail expansion across refresh/polling, and show recent run state clearly without requiring hidden terminals.

| Area | Page / Feature / Button / Menu Item | Test To Execute | Expected Result | Evidence Required | Safety / Blocker |
|---|---|---|---|---|---|
| Launch | Forge dashboard home | Start `python -m forge dashboard` and open `http://127.0.0.1:7777` | Page loads with Forge header, status cards, loop progress, experiment table, and steer panel | Raw + annotated launch screenshot, app log | Safe |
| Controls | Stop button | Verify Stop button is visible in top action area; click it once | Button remains responsive, API returns ok/stop requested, no visual break | Screenshot, automation log, API response | Safe; stop sentinel cleared after audit |
| Controls | Refresh button | Click Refresh | Dashboard refreshes current state without collapsing layout or console errors | Screenshot, console log | Safe |
| Experiments | Experiment row detail | Click an experiment row, wait for polling/refresh, verify detail remains open | Detail row opens quickly and remains expanded after refresh | Before/after screenshots and DOM state | Safe |
| Experiments | Zero-test filtering | Inspect experiment table rows | Rows with zero-test-only scores are not presented as top optimization signal | Screenshot/table text | Safe |
| Layout | Desktop responsive state | Inspect 1440x1000 viewport | Text/buttons are readable, no clipped primary controls, no horizontal overlap | Screenshot | Safe |
| Layout | Narrow viewport | Inspect 390x844 viewport | Dashboard remains usable with scrolling; controls not overlapping critical content | Screenshot | Safe |
| Steer | Message box | Inspect steer panel input and Send button without sending | Input and Send affordance visible and not clipped | Screenshot | Sending user messages skipped to avoid mutating run queue |
| Background process | Stop/retry state | Check process state and `/api/live` after Stop | Stop request is reflected without leaving orphaned child process from audit | Log/process record | Safe |
| External integrations | LM Studio/Vercel/etc. | No external deployment or LM Studio generation is triggered during UI audit | Marked Untested | N/A | Unsafe/expensive for visual audit scope |
