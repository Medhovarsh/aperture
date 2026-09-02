"""Render the README demo image from real CLI output.

Runs the two commands that define the product - the same question asked under two
different declared purposes - captures their actual output, and lays it out as a
side-by-side SVG terminal transcript.

Generating the image from live output rather than hand-writing it means the picture
in the README cannot drift away from what the tool actually prints.

Usage:
    python tools/render_demo.py [--out docs/demo.svg]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# Terminal geometry.
# Panels are sized from the widest line actually rendered, using the widest
# plausible monospace advance (DejaVu Sans Mono, 0.602em). Measuring beats
# guessing a constant: GitHub, browsers, and image viewers all pick different
# fallback fonts, and a too-narrow panel clips the output.
WORST_CASE_ADVANCE = 0.602
CHAR_WIDTH = 7.95
LINE_HEIGHT = 18.0
FONT_SIZE = 12.5
PADDING = 18.0
TITLEBAR = 30.0
PANEL_GAP = 18.0
MAX_COLUMNS = 70

THEME = {
    "bg": "#16161a",
    "chrome": "#22222a",
    "border": "#2e2e38",
    "text": "#d6d6dd",
    "muted": "#83839a",
    "prompt": "#5ec9a6",
    "command": "#e8e8ef",
    "allow": "#5ec9a6",
    "deny": "#ff8a75",
    "accent": "#8aa6ff",
    "title": "#9a9ab0",
}


def run_cli(args: list[str]) -> str:
    """Run the aperture CLI and return combined output."""
    env = {"PYTHONPATH": str(SRC), "PATH": ""}
    import os

    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(SRC)
    result = subprocess.run(
        [sys.executable, "-m", "aperture.cli", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environ,
        check=False,
    )
    return (result.stdout + result.stderr).rstrip("\n")


def classify(line: str) -> str:
    """Pick a color role for one output line."""
    stripped = line.strip()
    if stripped.startswith("$"):
        return "command"
    if stripped.startswith("trace ") or stripped.startswith("routed to:"):
        return "muted"
    if stripped.startswith("Withheld:"):
        return "deny"
    if " x " in stripped and any(
        code in stripped
        for code in ("purpose_not_permitted", "no_matching_rule", "tenant_mismatch",
                     "insufficient_clearance", "acl_mismatch", "stale", "budget_truncated")
    ):
        return "deny"
    if stripped.startswith("[") and "]" in stripped:
        return "allow"
    if stripped.startswith("redacted:") or stripped.startswith("note:"):
        return "accent"
    if "record(s) returned" in stripped:
        return "accent"
    if stripped.startswith("source="):
        return "muted"
    return "text"


def escape(text: str) -> str:
    """XML-escape a line of terminal output."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def wrap(lines: list[str], width: int = MAX_COLUMNS) -> list[str]:
    """Hard-wrap long lines so the panel width stays fixed."""
    wrapped: list[str] = []
    for line in lines:
        if len(line) <= width:
            wrapped.append(line)
            continue
        indent = " " * (len(line) - len(line.lstrip()) + 4)
        remaining = line
        first = True
        while remaining:
            take = width if first else width - len(indent)
            chunk, remaining = remaining[:take], remaining[take:]
            wrapped.append(chunk if first else indent + chunk.lstrip())
            first = False
    return wrapped


def panel_svg(x: float, y: float, title: str, lines: list[str], width: float, height: float) -> str:
    """Render one terminal panel."""
    parts = [
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="9" '
        f'fill="{THEME["bg"]}" stroke="{THEME["border"]}"/>',
        f'<path d="M{x} {y + 9} a9 9 0 0 1 9 -9 h{width - 18} a9 9 0 0 1 9 9 v{TITLEBAR - 9} '
        f'h-{width} z" fill="{THEME["chrome"]}"/>',
    ]
    for index, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        parts.append(f'<circle cx="{x + 18 + index * 15}" cy="{y + 15}" r="5.5" fill="{color}"/>')
    parts.append(
        f'<text x="{x + width / 2}" y="{y + 19}" text-anchor="middle" '
        f'fill="{THEME["title"]}" font-size="11.5" font-family="inherit">{escape(title)}</text>'
    )

    text_y = y + TITLEBAR + PADDING
    for line in lines:
        role = classify(line)
        if role == "command":
            body = escape(line.strip()[1:].strip())
            parts.append(
                f'<text x="{x + PADDING}" y="{text_y}" font-size="{FONT_SIZE}" '
                f'font-family="inherit"><tspan fill="{THEME["prompt"]}">$ </tspan>'
                f'<tspan fill="{THEME["command"]}">{body}</tspan></text>'
            )
        else:
            parts.append(
                f'<text x="{x + PADDING}" y="{text_y}" font-size="{FONT_SIZE}" '
                f'font-family="inherit" fill="{THEME[role]}" '
                f'xml:space="preserve">{escape(line)}</text>'
            )
        text_y += LINE_HEIGHT
    return "\n".join(parts)


def build(out_path: Path) -> Path:
    """Generate the demo SVG."""
    workspace = Path(tempfile.mkdtemp(prefix="aperture-demo-")) / "workspace"
    try:
        run_cli(["demo", "--path", str(workspace)])
        question = "how much parental leave do we offer"

        left_cmd = f"aperture query -p u_dana --purpose hr_support \"{question}\""
        left = [f"$ {left_cmd}", ""] + run_cli(
            ["query", "-w", str(workspace), "-p", "u_dana", "--purpose", "hr_support", question]
        ).splitlines()

        right_cmd = f"aperture query -p u_kim --purpose customer_support \"{question}\""
        right = [f"$ {right_cmd}", ""] + run_cli(
            [
                "query", "-w", str(workspace), "-p", "u_kim",
                "--purpose", "customer_support", question,
            ]
        ).splitlines()
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)

    left = wrap(left)
    right = wrap(right)

    widest = max((len(line) for line in (*left, *right)), default=MAX_COLUMNS)
    panel_width = PADDING * 2 + widest * FONT_SIZE * WORST_CASE_ADVANCE + 22
    rows = max(len(left), len(right))
    panel_height = TITLEBAR + PADDING * 2 + LINE_HEIGHT * rows
    caption_height = 54.0
    total_width = panel_width * 2 + PANEL_GAP
    total_height = panel_height + caption_height

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_width:.0f}" '
        f'height="{total_height:.0f}" viewBox="0 0 {total_width:.0f} {total_height:.0f}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">',
        f'<rect width="{total_width:.0f}" height="{total_height:.0f}" fill="none"/>',
        panel_svg(0, 0, "People Ops - purpose: hr_support", left, panel_width, panel_height),
        panel_svg(
            panel_width + PANEL_GAP, 0,
            "Support agent - purpose: customer_support", right, panel_width, panel_height,
        ),
        f'<text x="{total_width / 2}" y="{panel_height + 26}" text-anchor="middle" '
        f'font-size="13" fill="{THEME["muted"]}">'
        f'Same corpus. Same question. Different declared purpose.</text>',
        f'<text x="{total_width / 2}" y="{panel_height + 45}" text-anchor="middle" '
        f'font-size="13" fill="{THEME["deny"]}">'
        f'The support agent is told what it could not see, instead of quietly answering '
        f'from a thinner context.</text>',
        "</svg>",
    ]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(svg) + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "demo.svg")
    args = parser.parse_args()
    path = build(args.out)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
