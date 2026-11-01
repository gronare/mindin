from __future__ import annotations

from datetime import UTC, datetime

PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>notes-vault-mcp</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; background: #f6f5f2; color: #1d1c1a;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 26rem; margin: 4rem auto; padding: 2rem; background: #fff;
  border: 1px solid #e2e0da; border-radius: 10px; }}
h1 {{ margin: 0 0 .25rem; font-size: 1.25rem; }}
p {{ margin: 0 0 1.25rem; color: #56534d; }}
label {{ display: block; margin-bottom: .75rem; }}
input[type=email] {{ display: block; width: 100%; box-sizing: border-box; margin-top: .35rem;
  padding: .55rem .6rem; font-size: 1rem; border: 1px solid #cbc8c0; border-radius: 6px; }}
fieldset {{ margin: 0 0 1.25rem; padding: .75rem 1rem; border: 1px solid #e2e0da; border-radius: 6px; }}
legend {{ padding: 0 .35rem; color: #56534d; font-size: .85rem; }}
fieldset label {{ margin: 0; padding: .15rem 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
button {{ width: 100%; padding: .6rem; font-size: 1rem; color: #fff; background: #2f6f4f;
  border: 0; border-radius: 6px; cursor: pointer; }}
.error {{ margin-bottom: 1.25rem; padding: .6rem .75rem; color: #7a1f1f; background: #fbeceb;
  border: 1px solid #f0cfcc; border-radius: 6px; }}
</style>
</head>
<body>
<main>
<h1>{heading}</h1>
<p>{lead}</p>
{body}
</main>
</body>
</html>
"""


def stamp(moment: int | float | None) -> str:
    if not moment:
        return "-"
    return datetime.fromtimestamp(float(moment), tz=UTC).strftime("%Y-%m-%d %H:%M")
