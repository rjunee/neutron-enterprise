#!/usr/bin/env python3
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Wrap the pandoc HTML body in the Engineered Blueprint print shell.

Adds a cover page and a fixed per-page footer that Chrome's print
engine repeats on every rendered page. Usage: assemble.py <body.html> <out.html>
"""

# ruff: noqa: E501  — embedded CSS/HTML string literals legitimately exceed the line limit

import sys
from pathlib import Path

body = Path(sys.argv[1]).read_text()
out = Path(sys.argv[2])

FOOTER = "Neutron Enterprise · Apache License 2.0"

# The pandoc body opens with the H1/H2 title block; we render our own cover page
# and let the body flow after it.
COVER = """
<section class="cover">
  <div class="cover-rule"></div>
  <p class="cover-kicker">Technical Architecture White Paper</p>
  <h1 class="cover-title">Neutron Enterprise</h1>
  <p class="cover-sub">A sovereign, model-independent platform for deploying AI inside the enterprise</p>
  <div class="cover-meta">
    <p><strong>Version 1.4</strong> &nbsp;·&nbsp; 21 July 2026</p>
    <p>Prepared for system architects, security reviewers, and compliance &amp; regulatory reviewers</p>
  </div>
  <div class="cover-foot">
    <p>Copyright &copy; 2026 Ryan Junee. Licensed under the Apache License 2.0.</p>
  </div>
</section>
"""

CSS = """
@page {
  size: Letter;
  margin: 0.9in 0.85in 1.05in 0.85in;
}
:root {
  --paper:#f0f1f3; --ink:#14181f; --muted:#5b6472;
  --teal:#0f766e; --amber:#b0751a; --line:#c9ccd2; --rule:#d8dbe0;
}
* { box-sizing: border-box; }
html, body { margin:0; padding:0; background:#fff; color:var(--ink); }
body {
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  font-size: 10.6pt; line-height: 1.5; -webkit-print-color-adjust: exact; print-color-adjust: exact;
}

/* fixed footer: Chrome repeats position:fixed elements on every printed page */
.pagefoot {
  position: fixed; left: 0; right: 0; bottom: 0;
  text-align: center; font-family: "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 7.3pt; letter-spacing: 0.06em; color: var(--muted);
  text-transform: none;
}
.pagefoot .bar { display:inline-block; border-top:1px solid var(--rule); padding-top:3px; padding:3px 10px 0; }

/* cover page */
.cover { page-break-after: always; height: 8.6in; position: relative; padding-top: 1.2in; }
.cover-rule { width: 60px; height: 4px; background: var(--teal); margin-bottom: 0.5in; }
.cover-kicker {
  font-family:"SF Mono",ui-monospace,Menlo,monospace; font-size:9pt; letter-spacing:0.22em;
  text-transform:uppercase; color:var(--amber); margin:0 0 0.15in;
}
.cover-title {
  font-size: 46pt; line-height:1.02; margin:0 0 0.14in; font-weight:600; letter-spacing:-0.01em; border:0; padding:0;
}
.cover-sub { font-size: 15pt; color: var(--muted); font-style: italic; margin:0 0 0.7in; max-width: 5.4in; }
.cover-meta p { margin: 0 0 0.08in; font-size: 11pt; }
.cover-meta { margin-bottom: 1.1in; }
.cover-foot { position:absolute; bottom:0; left:0; }
.cover-foot p { margin:0; font-size:9pt; color:var(--muted); }
.cover-conf {
  font-family:"SF Mono",ui-monospace,Menlo,monospace !important; font-size:9pt !important;
  letter-spacing:0.14em; text-transform:uppercase; color:var(--amber) !important; margin-bottom:4px !important;
}

/* body typography */
h1, h2, h3, h4 { font-family:"Iowan Old Style", Palatino, Georgia, serif; font-weight:600; line-height:1.18; color:var(--ink); }
h1 { font-size: 22pt; margin: 0.2in 0 0.12in; padding-bottom:0.06in; border-bottom:2px solid var(--teal); page-break-before: always; }
h1:first-of-type { page-break-before: avoid; }
h2 { font-size: 15pt; margin: 0.28in 0 0.08in; color:var(--teal); }
h3 { font-size: 12pt; margin: 0.2in 0 0.05in; }
img { max-width: 100%; height: auto; display:block; margin: 0.18in auto 0.05in; page-break-inside: avoid; border:1px solid var(--rule); border-radius:4px; }
figure { margin: 0.2in 0; page-break-inside: avoid; }
figcaption { font-size: 9.5pt; color: var(--muted); font-style: italic; text-align:center; margin-top: 5px; }
h4 { font-size: 10.6pt; margin: 0.16in 0 0.04in; color:var(--muted); }
p { margin: 0 0 0.1in; orphans:3; widows:3; }
strong { font-weight:600; }
em { font-style: italic; }
a { color: var(--teal); text-decoration:none; }

blockquote {
  margin: 0.14in 0; padding: 0.1in 0.18in; border-left: 3px solid var(--amber);
  background:#faf6ee; color:var(--ink); font-size:10.4pt;
}
blockquote p:last-child { margin-bottom:0; }

ul, ol { margin: 0 0 0.12in; padding-left: 0.26in; }
li { margin-bottom: 0.045in; }

code {
  font-family:"SF Mono",ui-monospace,Menlo,monospace; font-size:8.9pt;
  background:#eceef1; padding:0.5px 4px; border-radius:3px;
}
pre {
  font-family:"SF Mono",ui-monospace,Menlo,monospace; font-size:8.2pt; line-height:1.4;
  background:var(--paper); border:1px solid var(--line); border-radius:5px;
  padding:0.12in 0.16in; overflow-x:auto; white-space:pre; page-break-inside:avoid;
}
pre code { background:none; padding:0; font-size:8.2pt; }

table {
  border-collapse: collapse; width:100%; margin:0.14in 0; font-size:9pt;
}
tr { page-break-inside: avoid; }
thead { display: table-header-group; }
th, td { border:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }
th { background:var(--paper); font-family:"SF Mono",ui-monospace,Menlo,monospace; font-size:8pt;
     letter-spacing:0.03em; text-transform:uppercase; color:var(--muted); font-weight:600; }
tr:nth-child(even) td { background:#f7f8f9; }

hr { border:0; border-top:1px solid var(--rule); margin:0.24in 0; }

/* keep headings with following content */
h2, h3, h4 { page-break-after: avoid; }
"""

HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Neutron Enterprise — Technical Architecture White Paper</title>
<style>{CSS}</style>
</head>
<body>
<div class="pagefoot"><span class="bar">{FOOTER}</span></div>
{COVER}
{body}
</body>
</html>
"""

out.write_text(HTML)
print(f"assembled {out}")
