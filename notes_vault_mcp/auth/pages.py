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


APP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · notes-vault-mcp</title>
<style>
:root {{ color-scheme: light; --bg: #f6f5f2; --panel: #fff; --line: #e2e0da; --line-strong: #cbc8c0;
  --ink: #1d1c1a; --muted: #56534d; --faint: #8a867e; --accent: #2f6f4f; --accent-soft: #e6f0ea;
  --warn: #7a1f1f; --mono: ui-monospace, SFMono-Regular, Menlo, monospace; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 16px max(16px, env(safe-area-inset-right)) 48px max(16px, env(safe-area-inset-left));
  background: var(--bg); color: var(--ink); font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
.app {{ max-width: 60rem; margin: 0 auto; background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; overflow: hidden; min-width: 0; }}
.top {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 12px 20px;
  border-bottom: 1px solid var(--line); }}
.mark {{ font-weight: 650; }}
.mark small {{ font-weight: 400; color: var(--faint); font-family: var(--mono); font-size: 12px; margin-left: 6px;
  overflow-wrap: anywhere; }}
.top nav {{ display: flex; gap: 4px; margin-left: auto; flex-wrap: wrap; }}
.top nav a {{ color: var(--muted); text-decoration: none; padding: 4px 10px; border-radius: 6px; font-size: 14px; }}
.top nav a.on {{ background: var(--bg); color: var(--ink); }}
.body {{ padding: 24px 20px 28px; display: grid; gap: 28px; min-width: 0; }}
h1 {{ margin: 0; font-size: 1.35rem; }}
h2 {{ margin: 0; font-size: 1rem; }}
p {{ margin: 0; }}
a {{ color: var(--accent); }}
.lead {{ margin-top: 4px; color: var(--muted); }}
.head {{ display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; }}
.muted {{ color: var(--muted); }}
.small {{ font-size: 13px; }}
code {{ font-family: var(--mono); font-size: 13px; overflow-wrap: anywhere; }}
.btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; font: inherit; font-size: 14px;
  border-radius: 6px; border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer;
  text-decoration: none; white-space: nowrap; }}
.btn.secondary {{ background: var(--panel); color: var(--ink); border-color: var(--line-strong); }}
.btn.danger {{ background: var(--panel); color: var(--warn); border-color: #e3b9b5; }}
.btn.small {{ padding: 3px 10px; font-size: 13px; }}
.btn:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {{
  outline: 2px solid var(--accent); outline-offset: 2px; }}
.chip {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 12px; line-height: 1.6;
  background: var(--accent-soft); color: var(--accent); border: 1px solid #c9ddd0; white-space: nowrap;
  font-weight: 400; }}
.group-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--faint);
  margin-bottom: 8px; }}
.vaults {{ display: grid; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
.vault {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 16px; padding: 14px 16px;
  border-top: 1px solid var(--line); text-decoration: none; color: inherit; }}
.vault:first-child {{ border-top: 0; }}
.vault:hover {{ background: #fbfaf8; }}
.vault .name {{ grid-column: 1; min-width: 0; overflow-wrap: anywhere; font-weight: 600; display: flex;
  gap: 8px; align-items: center; flex-wrap: wrap; }}
.vault .facts {{ grid-column: 1; min-width: 0; color: var(--muted); font-size: 13px;
  font-variant-numeric: tabular-nums; }}
.vault .where {{ grid-column: 1 / -1; font-family: var(--mono); font-size: 12px; color: var(--faint);
  overflow-wrap: anywhere; }}
.vault .go {{ grid-column: 2; grid-row: 1 / span 2; color: var(--faint); align-self: center; }}
.cols {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 16px; }}
.box {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; min-width: 0; display: grid;
  gap: 8px; align-content: start; }}
.copy {{ display: flex; gap: 8px; align-items: center; background: var(--bg); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 8px; min-width: 0; }}
.copy code {{ flex: 1; min-width: 0; }}
.tablewrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }}
th {{ text-align: left; font-weight: 500; color: var(--faint); font-size: 12px; text-transform: uppercase;
  letter-spacing: .05em; padding: 8px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
td {{ padding: 9px 12px; border-top: 1px solid var(--line); vertical-align: top; }}
tr:first-child td {{ border-top: 0; }}
td.nowrap {{ white-space: nowrap; }}
td form {{ margin: 0; }}
form {{ margin: 0; }}
.form {{ display: grid; gap: 14px; max-width: 34rem; }}
label.field {{ display: grid; gap: 4px; font-size: 14px; }}
input[type=text], input[type=email], select {{ width: 100%; padding: 8px 10px; font: inherit;
  border: 1px solid var(--line-strong); border-radius: 6px; background: var(--panel); color: var(--ink); }}
fieldset {{ border: 0; padding: 0; margin: 0; min-width: 0; }}
legend {{ margin-bottom: 6px; color: var(--muted); font-size: 13px; padding: 0; }}
.choice {{ display: grid; gap: 8px; }}
.option {{ display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; padding: 12px 14px;
  border: 1px solid var(--line); border-radius: 8px; cursor: pointer; }}
.option:has(input:checked) {{ border-color: var(--accent); background: #f7fbf8; }}
.option input {{ margin-top: 4px; }}
.option b {{ font-weight: 600; }}
.option span {{ grid-column: 2; color: var(--muted); font-size: 13px; }}
.row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
.row .field {{ flex: 1; min-width: 12rem; }}
.row .field.narrow {{ flex: 0 1 12rem; min-width: 9rem; }}
ul.plain {{ list-style: none; margin: 0; padding: 0; }}
.danger-zone {{ border: 1px solid #efd3d0; border-radius: 8px; padding: 14px 16px; display: grid; gap: 10px;
  align-content: start; min-width: 0; }}
details > summary {{ list-style: none; }}
details > summary::-webkit-details-marker {{ display: none; }}
details[open] > summary {{ margin-bottom: 10px; }}
.kv {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px; margin: 0; font-size: 14px; }}
.kv dt {{ color: var(--faint); }}
.kv dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere; }}
.breadcrumb {{ font-size: 13px; color: var(--faint); margin-bottom: 4px; }}
.breadcrumb a {{ color: var(--muted); }}
.stack {{ display: grid; gap: 10px; min-width: 0; }}
.notice {{ padding: 10px 12px; border-radius: 6px; background: #fdf5e2; border: 1px solid #f0e0b5; color: #6b4a00;
  display: grid; gap: 8px; }}
.adding {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: end; }}
.adding label {{ display: grid; gap: 4px; font-size: 14px; flex: 1; min-width: 12rem; }}
.adding .error {{ flex-basis: 100%; }}
button.secondary:not(.btn) {{ padding: 7px 14px; font: inherit; font-size: 14px; border-radius: 6px;
  border: 1px solid var(--line-strong); background: var(--panel); color: var(--ink); cursor: pointer; }}
.error {{ padding: 10px 12px; border-radius: 6px; color: var(--warn); background: #fbeceb;
  border: 1px solid #f0cfcc; }}
@media (max-width: 560px) {{
  .top nav {{ margin-left: 0; width: 100%; }}
  .body {{ padding: 18px 14px 22px; }}
  .vault .go {{ display: none; }}
  .vault .name, .vault .facts {{ grid-column: 1 / -1; }}
  .tablewrap thead {{ display: none; }}
  .tablewrap tr {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; column-gap: 12px; padding: 8px 0;
    border-top: 1px solid var(--line); }}
  .tablewrap tr:first-child {{ border-top: 0; }}
  .tablewrap td {{ border: 0; padding: 1px 12px; grid-column: 1; }}
  .tablewrap td:first-child {{ font-weight: 600; }}
  .tablewrap td:not(:first-child) {{ color: var(--muted); font-size: 13px; }}
  .tablewrap td:last-child {{ grid-column: 2; grid-row: 1 / span 5; align-self: center; }}
}}
</style>
</head>
<body>
<div class="app">
<div class="top"><span class="mark">Vault <small>{host}</small></span>
<nav>{nav}</nav></div>
<div class="body">
{body}
</div>
</div>
<script src="{script}" defer></script>
</body>
</html>
"""

APP_SCRIPT = """document.querySelectorAll("[data-copy]").forEach(function (button) {
  button.hidden = false;
  button.addEventListener("click", function () {
    var text = document.getElementById(button.dataset.copy).textContent;
    navigator.clipboard.writeText(text).then(function () { button.textContent = "Copied"; }, function () {});
  });
});
document.querySelectorAll("[data-slug-from]").forEach(function (target) {
  var field = document.getElementById(target.dataset.slugFrom);
  function show() {
    var slug = field.value.normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
    target.textContent = target.dataset.base + (slug || "…") + "/mcp";
  }
  field.addEventListener("input", show);
  show();
});
"""
