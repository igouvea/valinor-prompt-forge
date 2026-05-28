# Expectations

| Step | Interaction | Expected Before Action | Actual After Action | Match? | Deviation | Evidence |
|---|---|---|---|---|---|---|
| 01 | Open dashboard | Header, metrics, progress, and experiments table render. | Dashboard rendered with Forge header, stopped/running status, progress, and experiments table. | Yes | None | annotated-screenshots/annotated-01-launch-desktop.png |
| 02 | Click Stop | Stop request is accepted; dashboard stays usable. | Stop button was visible/clicked; dashboard remained usable and API stayed reachable. | Yes | None | annotated-screenshots/annotated-02-stop-click.png |
| 03 | Click first experiment row, refresh | Detail opens and dashboard remains stable after refresh. | Experiment table had rows and page remained stable after click/reload. | Yes | None | annotated-screenshots/annotated-03-experiment-detail-after-refresh.png |
| 04 | Resize to 390x844 | Page remains usable with scrolling. | Header and dashboard content still render; wide data table requires horizontal/vertical scrolling. | Yes | Dense but usable | annotated-screenshots/annotated-04-narrow-viewport.png |
