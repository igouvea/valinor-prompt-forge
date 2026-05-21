"""`uv run forge ...` / `python -m forge ...` dispatch."""

from __future__ import annotations

import sys

from . import experiment


USAGE = """\
Usage: forge <command> [args]

Commands:
  eval       Run one experiment with the current champion prompts; print result.
  run        (not yet implemented) Outer autoresearch loop.
  promote    (not yet implemented) Write champion prompts back to Valinor.
  dashboard  (not yet implemented) Start the live dashboard.

Examples:
  uv run forge eval
"""


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "eval":
        return experiment.main(rest)
    if cmd in {"run", "promote", "dashboard"}:
        print(f"[forge] '{cmd}' is not yet implemented (planned in later phases).")
        return 2
    print(f"[forge] unknown command: {cmd}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
