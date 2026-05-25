"""valinor-prompt-forge: autoresearch loop for Valinor agent prompts."""

import sys as _sys

# The console output uses arrows/checkmarks (→ ✓ ✗ ≈ ≥). Windows' default
# cp1252 stdout raises UnicodeEncodeError on those, which would crash an
# overnight run mid-print. Force UTF-8 with replacement so output never fails.
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

__version__ = "0.1.0"
