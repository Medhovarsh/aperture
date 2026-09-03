"""Vercel serverless entrypoint.

Vercel's Python runtime discovers an ASGI application named `app` in this module,
and vercel.json rewrites every route here, so one function serves both the UI and
the JSON API.

The package is shipped via `includeFiles` rather than installed from
pyproject.toml. Installing it made the build resolve dependencies from the project
metadata, where fastapi is an optional extra, and the function then failed to
import at runtime. Putting src/ on the path keeps what ships explicit.

The real product is the MCP stdio server; this hosts only the demo playground.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aperture.playground import app  # noqa: E402,F401  (re-exported for the runtime)
