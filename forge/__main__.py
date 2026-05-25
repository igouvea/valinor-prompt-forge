"""`uv run forge ...` / `python -m forge ...` dispatch."""

from __future__ import annotations

import sys


USAGE = """\
Usage: forge <command> [args]

Commands:
  eval               Run ONE experiment with the current champion prompts; print result.
  run [--max N]      Outer autoresearch loop: propose → run → judge → ratchet → loop.
                     --max N stops after N experiments (handy for a mini-loop).
  stop               Signal a running loop to stop after the current experiment.
  dashboard          Start the live GUI at http://127.0.0.1:7777.
  promote [--apply]  Write champion prompts into Valinor. Dry-run unless --apply.

Examples:
  uv run forge run --max 5
  uv run forge dashboard
  uv run forge promote --apply
"""


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "eval":
        from . import experiment
        return experiment.main(rest)
    if cmd == "run":
        from . import orchestrator
        return orchestrator.main(rest)
    if cmd == "stop":
        from . import state
        state.request_stop()
        print("[forge] stop requested — the loop will exit after the current experiment.")
        return 0
    if cmd == "dashboard":
        from . import dashboard
        return dashboard.main(rest)
    if cmd == "promote":
        from . import promote
        return promote.main(rest)

    print(f"[forge] unknown command: {cmd}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
