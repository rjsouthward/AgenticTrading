"""
Render a FlagSession to a self-contained HTML file for live browser preview.

Reads the React (TSX) artifact template, inlines the session payload, wraps it
in an HTML shell that loads React + Babel-standalone from a CDN, and writes the
result. Open the file in any browser — no build step.

Usage:
    python -m blind_spot.flag_stream.preview <session_id> [--out PATH] [--open]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from blind_spot.flag_stream.persistence import load_flags

load_dotenv()

_ARTIFACT_PATH = Path(__file__).parent / "artifact.tsx"
_DATA_TOKEN    = "__FLAG_DATA__"

_HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Blind Spot Flags — {session_id}</title>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>html,body,#root{{margin:0;padding:0;height:100%;}}</style>
</head>
<body>
  <div id="root"></div>
  <script type="text/babel" data-presets="react,typescript">
const {{ useState, useMemo, Fragment }} = React;

{tsx}

const root = ReactDOM.createRoot(document.getElementById("root"));
root.render(<BlindSpotFlags />);
  </script>
</body>
</html>
"""


def render(session_id: str) -> str:
    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
    )
    try:
        payload = load_flags(
            driver, session_id, database=os.getenv("NEO4J_DATABASE", "neo4j")
        )
    finally:
        driver.close()

    if payload is None:
        sys.exit(f"no FlagSession with session_id '{session_id}'")

    tsx = _ARTIFACT_PATH.read_text(encoding="utf-8")
    tsx = tsx.replace(_DATA_TOKEN, json.dumps(payload, indent=2, default=str), 1)
    # Strip imports and `export default` — Babel-standalone runs in browser
    # with React already on `window`, no module system.
    tsx = "\n".join(
        line for line in tsx.splitlines()
        if not line.startswith("import ")
    )
    tsx = tsx.replace("export default function", "function")
    # Hooks come in via the destructured import in the .tsx source; after
    # stripping that import, rewrite bare hook references to use the React global
    # so the browser shell doesn't need a bundler. React.Fragment already works.
    tsx = tsx.replace("React.Fragment", "Fragment")

    return _HTML_SHELL.format(session_id=session_id, tsx=tsx)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("session_id")
    p.add_argument("--out", default="/tmp/blind_spot_preview.html",
                   help="output HTML path (default /tmp/blind_spot_preview.html)")
    p.add_argument("--open", action="store_true",
                   help="open the file in the default browser when done")
    args = p.parse_args()

    html = render(args.session_id)
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}")
    if args.open:
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
