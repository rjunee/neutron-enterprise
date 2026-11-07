#!/usr/bin/env bash
# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
#
# Build the Neutron Enterprise architecture white paper PDF from markdown.
# Pipeline: pandoc (md -> html fragment) -> HTML shell (house style + per-page
# footer) -> Chrome headless --print-to-pdf.
#
# Deps: pandoc, Google Chrome. No LaTeX required.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MD="$DIR/neutron-enterprise-architecture-whitepaper.md"
BODY="$DIR/.body.html"
HTML="$DIR/neutron-enterprise-architecture-whitepaper.html"
PDF="$DIR/neutron-enterprise-architecture-whitepaper.pdf"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 1. markdown -> html body fragment (frontmatter consumed as metadata, not rendered)
pandoc "$MD" -f markdown -t html5 --no-highlight -o "$BODY"

# 2. assemble the styled, print-ready HTML shell
python3 "$DIR/assemble.py" "$BODY" "$HTML"

# 3. render to PDF (no Chrome default header/footer; our CSS footer repeats per page)
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$PDF" "file://$HTML" >/dev/null 2>&1

rm -f "$BODY"
echo "PDF: $PDF"
ls -lh "$PDF" | awk '{print $5, $NF}'
