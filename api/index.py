"""Vercel serverless entrypoint.

Vercel's Python runtime discovers an ASGI application named `app` in this module.
All routes are rewritten here by vercel.json, so this single function serves both
the UI and the JSON API.

The real product is the MCP stdio server; this hosts only the demo playground.
"""

from aperture.playground import app  # noqa: F401  (re-exported for the runtime)
