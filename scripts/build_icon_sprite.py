"""Download the Lucide icons the UI uses and write them as one SVG sprite.

Run once after changing ICONS: .venv/bin/python scripts/build_icon_sprite.py (the output is committed).
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

VERSION = "0.544.0"
ICONS = [
    "activity", "arrow-left", "chart-candlestick", "check", "circle-alert", "database", "download", "ellipsis",
    "inbox", "layout-dashboard", "lock", "menu", "pencil", "plus", "refresh-cw", "search", "settings", "sun-moon",
    "trash-2", "x",
]
OUT = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "vendor" / "icons.svg"


def main() -> None:
    symbols = []
    for name in ICONS:
        url = f"https://cdn.jsdelivr.net/npm/lucide-static@{VERSION}/icons/{name}.svg"
        svg = urllib.request.urlopen(url, timeout=30).read().decode()
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
        symbols.append(f'<symbol id="i-{name}" viewBox="0 0 24 24">{" ".join(inner.split())}</symbol>')
    OUT.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg">\n<!-- Lucide v{VERSION} (ISC), see LICENSE-lucide.txt -->\n'
        + "\n".join(symbols)
        + "\n</svg>\n"
    )
    print(f"wrote {len(symbols)} icons to {OUT}")


if __name__ == "__main__":
    main()
