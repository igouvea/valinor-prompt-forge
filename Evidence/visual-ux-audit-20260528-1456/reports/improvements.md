# Improvements

| Type | Area | Recommendation | User Benefit | Evidence / Rationale | Suggested Priority |
|---|---|---|---|---|---|
| UX | Experiment table | Add compact mobile cards or a column picker for narrow screens | Easier mobile inspection without horizontal scanning | annotated-screenshots/annotated-04-narrow-viewport.png | Medium |
| Reliability | Forge loop | Show last failed role and held-out failure summary directly in dashboard top status | Faster diagnosis after local LM Studio failures | exp-0082 failed correctly but details require logs/result JSON | Medium |
| Instrumentation | Agent runtime | Add a visible "last compacted handoff" and capped-file count to run logs/dashboard | Confirms context compaction happened without opening files | New cap behavior is currently proven by unit tests/logs | Low |
| Feature | Dashboard | Add a safe "Clear stop flag" button distinct from Stop | Lets operator recover after testing Stop without CLI | Audit clicked Stop and needed CLI clear after | Low |
