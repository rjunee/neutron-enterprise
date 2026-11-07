# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Offline goldens generator for the token-estimator oracle (NE-CTXB-1).

Run MANUALLY (never imported by any test, never a shipped dependency)::

    uv run --with tiktoken --with tokenizers \
        python packages/ne-contracts/tests/fixtures/token_oracle/generate_goldens.py

It downloads the deployed-lane Qwen3 tokenizer (Apache-2.0) and loads the
frontier-proxy ``o200k_base`` BPE (tiktoken, MIT), counts every vendored
multi-script fixture with BOTH, and SELECTS ``DEFAULT_BYTES_PER_TOKEN`` as the
largest divisor on the ladder that keeps ``ByteTokenEstimator(candidate, 8)``
one-sided-conservative with >=10% generation-time headroom on every sample. The
result is written to ``goldens.json`` (checked in), which the hermetic test
``test_token_estimator_oracle.py`` reads WITHOUT any tokenizer import — CI never
needs egress or the tokenizer libraries.

If even the smallest ladder rung fails a sample, the script STOPS and prints a
``brief-gap:`` blocker rather than fabricating a divisor or a count.
"""

# The CJK fixtures below intentionally use native fullwidth punctuation, which is
# the real text the tokenizer counts, so the ambiguous-character lint is off here.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

QWEN_REPO = "Qwen/Qwen3-0.6B"
QWEN_FILE = "tokenizer.json"
QWEN_URL = f"https://huggingface.co/{QWEN_REPO}/resolve/main/{QWEN_FILE}"
O200K_ENCODING = "o200k_base"

PER_CALL_OVERHEAD = 8
# Ladder: largest first. The chosen divisor is the largest rung that stays
# one-sided with >=HEADROOM slack on EVERY sample.
LADDER = [3.0, 2.8, 2.5, 2.2, 2.0, 1.8, 1.5]
HEADROOM = 1.10  # 10% generation-time margin over the max real count

# Five script classes, >=4 realistic samples each. The URI class carries the
# planner-probed worst cases: demo-shaped urn:ne:entity table rows and a
# percent-encoded SPARQL HTTP URL.
FIXTURES: dict[str, list[str]] = {
    "latin": [
        (
            "The quarterly review confirmed that the northeast region exceeded "
            "its revenue target by eleven percent, driven largely by renewals "
            "in the mid-market segment and two strategic enterprise wins."
        ),
        (
            "Please reconcile the outstanding invoices against the purchase "
            "orders before Friday; the finance team needs the aged-receivables "
            "summary for the board deck and cannot close the month without it."
        ),
        (
            "Our onboarding flow now provisions a sandbox tenant automatically, "
            "so new customers can evaluate the connector suite against their own "
            "data within minutes rather than waiting on a manual environment."
        ),
        (
            "Escalation policy: if a governed tool call is refused twice in a "
            "single session, the agent must surface the typed error to the "
            "operator and stop retrying rather than silently degrading output."
        ),
    ],
    "cjk": [
        "本季度的经营评审确认华东区域的营业收入超过既定目标百分之十一，"
        "主要得益于中端市场的续约以及两笔重要的企业级新签合同。",
        "请在周五之前完成未结算发票与采购订单的核对；财务团队需要账龄应收"
        "汇总表用于董事会材料，否则本月无法顺利结账。",
        "四半期のレビューにより、北東地域が売上目標を十一パーセント上回った"
        "ことが確認されました。中堅市場の更新と二件の大型契約が主な要因です。",
        "新しいオンボーディング手順ではサンドボックス環境を自動的に払い出す"
        "ため、顧客は自社データでコネクタ群を数分で評価できるようになります。",
    ],
    "emoji": [
        "Great work team! 🎉🎉 shipping the new dashboard today 🚀 — grab a "
        "coffee ☕ and let's celebrate 🥳 before the next sprint kicks off.",
        "family trip recap 👨‍👩‍👧‍👦 we hiked 🥾, camped ⛺, and watched the "
        "sunset 🌅 — best weekend in months, already planning the next one 😄.",
        "status: 🔴 blocked on infra, 🟡 review pending, 🟢 tests green — "
        "will 🇺🇸 sync at 9 and 🇯🇵 sync at 5 so both regions are covered 👍.",
        "she said 👋🏽 hello and waved 🙋🏾‍♀️ from across the room; the whole "
        "crew 🧑🏻‍💻🧑🏽‍🔧🧑🏿‍🚀 showed up for the launch party 🎊🎈🎂.",
    ],
    "code": [
        "def input_budget(window: int, out: int, margin: int) -> int:\n"
        "    if out + margin >= window:\n"
        '        raise ValueError("budget must be positive")\n'
        "    return window - out - margin",
        "SELECT ?account (COUNT(?order) AS ?orders)\n"
        "WHERE { ?account a :Account ; :placed ?order . }\n"
        "GROUP BY ?account\n"
        "ORDER BY DESC(?orders)\n"
        "LIMIT 25",
        '{"stage": "runtime-trim", "estimated_tokens": 31000, '
        '"input_budget": 27648, "context_window": 32768, "dropped_turns": 12, '
        '"model": "qwen3-0.6b", "lane": "deployed"}',
        'curl -sS -X POST "$GATEWAY/v1/chat/completions" '
        '-H "authorization: Bearer $TOKEN" '
        "-d @payload.json | jq '.usage.prompt_tokens'",
    ],
    "uri": [
        "urn:ne:entity:acct-00042 | Acme Corporation | 17 placed orders\n"
        "urn:ne:entity:acct-00043 | Beacon Logistics | 4 placed orders\n"
        "urn:ne:entity:acct-00044 | Corveto Holdings | 29 placed orders\n"
        "urn:ne:entity:acct-00045 | Delphi Materials | 8 placed orders",
        "urn:ne:entity:order-10231 | acct-00042 | 2026-05-14 | fulfilled\n"
        "urn:ne:entity:order-10232 | acct-00042 | 2026-05-19 | fulfilled\n"
        "urn:ne:entity:order-10233 | acct-00043 | 2026-06-02 | pending\n"
        "urn:ne:entity:order-10234 | acct-00044 | 2026-06-11 | cancelled",
        "https://graph.internal/sparql?query=SELECT%20%3Facct%20%3Fname%20"
        "WHERE%20%7B%20%3Facct%20a%20%3AAccount%20%3B%20rdfs%3Alabel%20%3Fname"
        "%20%7D%20ORDER%20BY%20%3Fname%20LIMIT%2050&format=application%2Fsparql"
        "-results%2Bjson",
        "urn:ne:entity:acct-00042\nurn:ne:entity:acct-00043\n"
        "urn:ne:entity:acct-00044\nurn:ne:entity:order-10231\n"
        "urn:ne:entity:order-10232\nurn:ne:entity:policy-acl-eu-west",
    ],
}


def _estimate(text: str, bytes_per_token: float, overhead: int) -> int:
    return math.ceil(len(text.encode("utf-8")) / bytes_per_token) + overhead


def _download_qwen_tokenizer() -> tuple[Any, str, bytes]:
    req = urllib.request.Request(QWEN_URL, headers={"User-Agent": "ne-oracle/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    sha256 = hashlib.sha256(data).hexdigest()
    from tokenizers import Tokenizer  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        fh.write(data)
        tmp_path = fh.name
    tok = Tokenizer.from_file(tmp_path)
    Path(tmp_path).unlink(missing_ok=True)
    return tok, sha256, data


def main() -> int:
    import tiktoken  # type: ignore[import-not-found]
    import tokenizers  # type: ignore[import-not-found]

    qwen_tok, qwen_sha256, _ = _download_qwen_tokenizer()
    o200k = tiktoken.get_encoding(O200K_ENCODING)

    samples: list[dict[str, Any]] = []
    for script_class, texts in FIXTURES.items():
        for idx, text in enumerate(texts):
            qwen_ct = len(qwen_tok.encode(text, add_special_tokens=False).ids)
            o200k_ct = len(o200k.encode(text))
            if qwen_ct <= 0 or o200k_ct <= 0:
                print(f"brief-gap: zero token count for {script_class}[{idx}]", file=sys.stderr)
                return 1
            samples.append(
                {
                    "id": f"{script_class}-{idx}",
                    "script_class": script_class,
                    "text": text,
                    "actual_tokens": {"qwen3": qwen_ct, "o200k_base": o200k_ct},
                }
            )

    # Ladder selection: largest divisor keeping >=HEADROOM slack on every sample.
    chosen: float | None = None
    for candidate in LADDER:
        ok = True
        for s in samples:
            need = math.ceil(HEADROOM * max(s["actual_tokens"].values()))
            if _estimate(s["text"], candidate, PER_CALL_OVERHEAD) < need:
                ok = False
                break
        if ok:
            chosen = candidate
            break
    if chosen is None:
        print(
            "brief-gap: no ladder rung (down to 1.5) satisfies the "
            "10%-headroom one-sidedness invariant on every fixture; the brief's "
            "byte-divisor approach cannot cover these script classes — escalate.",
            file=sys.stderr,
        )
        return 1

    # Per-class minimum margin at the chosen divisor (estimate / max-actual).
    per_class_min: dict[str, float] = {}
    for s in samples:
        est = _estimate(s["text"], chosen, PER_CALL_OVERHEAD)
        margin = est / max(s["actual_tokens"].values())
        cur = per_class_min.get(s["script_class"])
        if cur is None or margin < cur:
            per_class_min[s["script_class"]] = round(margin, 4)

    goldens = {
        "meta": {
            "generated_date": date.today().isoformat(),
            "qwen_tokenizer": {
                "repo": QWEN_REPO,
                "file": QWEN_FILE,
                "sha256": qwen_sha256,
            },
            "o200k": {"tiktoken_version": tiktoken.__version__, "encoding": O200K_ENCODING},
            "tokenizers_version": tokenizers.__version__,
            "chosen_bytes_per_token": chosen,
            "per_call_overhead": PER_CALL_OVERHEAD,
            "headroom_at_generation": HEADROOM,
            "per_class_min_margin": per_class_min,
        },
        "samples": samples,
    }

    out_path = Path(__file__).parent / "goldens.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(goldens, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"wrote {out_path} — chosen_bytes_per_token={chosen}")
    print(f"per-class min margin: {per_class_min}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
