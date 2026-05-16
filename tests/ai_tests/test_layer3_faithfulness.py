"""Layer 3 — Narrative faithfulness via Haiku (split) + Sonnet (judge).

For the four AI-written narrative blocks per fixture:
  - range_overview         (2-4 paragraphs of marketing copy)
  - sales_arguments        (joined into one block — bullets pack multiple
                            facts each, so Haiku splits them further)
  - seo_long_description   (~1000 chars of SEO copy)
  - meta_description       (~150 chars short blurb)

Pipeline per block:
  1. Haiku splits the text into atomic factual claims (1 fact each).
  2. Sonnet judges each claim against the sources (proforma + web_context).
  3. Faithfulness = supported_claims / total_claims.

The aggregate faithfulness is the headline number to watch over time.

Cost: ~$0.50-0.60 first run, $0 after caching.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from conftest import Fixture, RUNS_DIR, run_pipeline
import judge
import judge_cache


# ── Narrative block extraction ───────────────────────────────────────────────

def _narrative_blocks(pis_data: dict) -> list[tuple[str, str]]:
    """Return [(block_name, text), ...] for each AI-written narrative block
    with substantial content (>= MIN_BLOCK_CHARS)."""
    MIN_BLOCK_CHARS = 50

    out: list[tuple[str, str]] = []

    range_ov = (pis_data.get("range_overview") or "").strip()
    if len(range_ov) >= MIN_BLOCK_CHARS:
        out.append(("range_overview", range_ov))

    sales = pis_data.get("sales_arguments") or []
    if isinstance(sales, list) and sales:
        # Join into one block — Haiku will split into per-fact claims.
        joined = "\n".join(f"- {s}" for s in sales if isinstance(s, str))
        if len(joined) >= MIN_BLOCK_CHARS:
            out.append(("sales_arguments", joined))

    seo = pis_data.get("seo_data") or {}
    seo_long = (seo.get("seo_long_description") or "").strip()
    if len(seo_long) >= MIN_BLOCK_CHARS:
        out.append(("seo_long_description", seo_long))

    meta_desc = (seo.get("meta_description") or "").strip()
    if len(meta_desc) >= MIN_BLOCK_CHARS:
        out.append(("meta_description", meta_desc))

    return out


# ── Cached claim extraction (Haiku) ──────────────────────────────────────────

def _extract_claims_cached(
    fixture: Fixture,
    block_name: str,
    text: str,
    srcs_h: str,
) -> list[str]:
    cache_path = fixture.dir / "judge_layer3_cache.json"
    payload = {
        "block": block_name,
        # Hash-equivalent: the text itself goes into the key via judge_cache._key
        "text_excerpt": text[:80],
        "text_len": len(text),
    }
    # The full text needs to be in the cache key (not just excerpt). Include it.
    payload["text"] = text

    def _call_haiku():
        return {"claims": judge.extract_claims(fixture.product_name, text)}

    verdict = judge_cache.judged_or_run(
        cache_path=cache_path,
        judge="haiku_claim_extract",
        srcs_hash=srcs_h,
        payload=payload,
        run_fn=_call_haiku,
    )
    return verdict.get("claims") or []


# ── Cached claim judging (Sonnet) ────────────────────────────────────────────

def _judge_claim_cached(
    fixture: Fixture,
    claim: str,
    proforma_text: str,
    web_context: str,
    srcs_h: str,
) -> dict:
    cache_path = fixture.dir / "judge_layer3_cache.json"
    payload = {"claim": claim}

    def _call_sonnet():
        return judge.judge_claim(claim, proforma_text, web_context)

    return judge_cache.judged_or_run(
        cache_path=cache_path,
        judge="sonnet_claim_judge",
        srcs_hash=srcs_h,
        payload=payload,
        run_fn=_call_sonnet,
    )


# ── Per-fixture faithfulness scoring ────────────────────────────────────────

def _score_fixture(fixture: Fixture) -> dict:
    pis_data = run_pipeline(fixture)
    proforma_text = fixture.proforma_text
    web_context = pis_data.get("_web_context") or ""
    srcs_h = judge_cache.sources_hash(proforma_text, web_context)

    blocks = _narrative_blocks(pis_data)
    per_block: list[dict] = []
    total_claims = 0
    total_supported = 0

    for block_name, text in blocks:
        claims = _extract_claims_cached(fixture, block_name, text, srcs_h)
        block_supported = 0
        block_unsupported: list[dict] = []

        for c in claims:
            verdict = _judge_claim_cached(fixture, c, proforma_text, web_context, srcs_h)
            if verdict.get("supported"):
                block_supported += 1
            else:
                block_unsupported.append({
                    "claim": c,
                    "reason": verdict.get("reason", ""),
                })

        per_block.append({
            "block": block_name,
            "total_claims": len(claims),
            "supported": block_supported,
            "faithfulness": (block_supported / len(claims)) if claims else 1.0,
            "unsupported_claims": block_unsupported,
        })
        total_claims += len(claims)
        total_supported += block_supported

    return {
        "name": fixture.name,
        "blocks": per_block,
        "total_claims": total_claims,
        "supported": total_supported,
        "faithfulness": (total_supported / total_claims) if total_claims else 1.0,
    }


# ── Per-fixture test (soft — records, never fails individually) ─────────────

@pytest.mark.needs_anthropic
def test_faithfulness_per_fixture(fixture: Fixture):
    """Soft per-fixture record. Fixtures with empty narratives skip.
    The aggregate test is the gate.

    Sanity assertion: if the fixture has substantial narrative blocks but
    zero claims were extracted, the Haiku splitter silently failed (e.g. an
    API error caused the function to return an empty list). Fail loud rather
    than report a vacuous 100% faithfulness.
    """
    pis_data = run_pipeline(fixture)
    blocks = _narrative_blocks(pis_data)
    if not blocks:
        pytest.skip("no substantial narrative blocks to judge")

    result = _score_fixture(fixture)

    assert result["total_claims"] > 0, (
        f"[{fixture.name}] {len(blocks)} narrative block(s) present "
        f"(total {sum(len(t) for _, t in blocks)} chars) but 0 atomic claims "
        f"extracted — the Haiku claim splitter silently failed. Check the "
        f"judge.py error log above, then clear "
        f"`fixtures/{fixture.name}/judge_layer3_cache.json` and re-run."
    )

    print(
        f"\n  [{fixture.name}] claims={result['total_claims']} "
        f"supported={result['supported']} "
        f"faithfulness={result['faithfulness']:.1%}"
    )
    for b in result["blocks"]:
        print(
            f"    - {b['block']:25s} {b['supported']}/{b['total_claims']} "
            f"({b['faithfulness']:.0%})"
        )


# ── Aggregate summary (the hard gate) ───────────────────────────────────────

@pytest.mark.needs_anthropic
def test_layer3_summary(all_fixtures: list[Fixture]):
    """Aggregate faithfulness across every fixture. Loose threshold on the
    first runs — tighten once baseline is known."""

    FAITHFULNESS_THRESHOLD = 0.50   # generous; per plan target is 0.85

    summary: dict[str, Any] = {
        "layer": 3,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": FAITHFULNESS_THRESHOLD,
        "fixtures": [],
        "totals": {"total_claims": 0, "supported": 0},
    }

    for f in all_fixtures:
        pis_data = run_pipeline(f)
        if not _narrative_blocks(pis_data):
            summary["fixtures"].append({"name": f.name, "skipped": "no narrative"})
            continue

        result = _score_fixture(f)
        summary["fixtures"].append({
            "name": result["name"],
            "total_claims": result["total_claims"],
            "supported": result["supported"],
            "faithfulness": round(result["faithfulness"], 4),
            "blocks": [
                {k: v for k, v in b.items() if k != "unsupported_claims"}
                for b in result["blocks"]
            ],
            "unsupported_examples": [
                {"block": b["block"], "claim": uc["claim"], "reason": uc["reason"]}
                for b in result["blocks"]
                for uc in b["unsupported_claims"][:3]   # top 3 per block to keep report readable
            ],
        })
        summary["totals"]["total_claims"] += result["total_claims"]
        summary["totals"]["supported"] += result["supported"]

    t = summary["totals"]
    overall = (t["supported"] / t["total_claims"]) if t["total_claims"] else 1.0
    summary["overall_faithfulness"] = round(overall, 4)
    summary["anthropic_cost"] = judge.get_call_log().summary()

    safe_ts = summary["timestamp"].replace(":", "-")
    report_path = RUNS_DIR / f"layer3_{safe_ts}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"\n[Layer 3] {len(all_fixtures)} fixtures | "
        f"claims={t['total_claims']} | supported={t['supported']} | "
        f"faithfulness={overall:.1%}\n"
        f"  Anthropic spend this run: ${summary['anthropic_cost']['total_cost_usd']:.4f}\n"
        f"  Report: {report_path.relative_to(report_path.parents[2])}"
    )

    assert overall >= FAITHFULNESS_THRESHOLD, (
        f"Layer 3 faithfulness is {overall:.1%} "
        f"(threshold {FAITHFULNESS_THRESHOLD:.0%}). See {report_path.name}."
    )
