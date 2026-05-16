"""Layer 2 — Web-grounded check + Haiku paraphrase rescue.

Two passes:

  Pass A (grep, free)
     Every field tagged `web_grounded` must appear in the frozen
     web_context.txt. This is a sanity check on the classifier itself.

  Pass B (Claude Haiku, ~$0.30/full run, cached afterwards)
     For every field tagged `hallucinated` by the grep, ask Haiku whether
     the value is paraphrased anywhere in the sources. If yes, rescue it
     to `web_grounded` (the grep was just too strict on unit conversions
     or abbreviations). If no, confirmed hallucination.

Reports two rates:
  - hallucination_rate_grep   = grep_hallucinated / classified_fields
  - hallucination_rate_judged = (grep_hallucinated - rescued) / classified_fields

The judged rate is the metric to watch over time. The hard-fail gate at
the summary level is loose (50%) for the first runs — tighten once you
know the baseline.

`classified_fields` excludes pure-AI narrative slots (range_overview,
sales_arguments, seo_*) because the classifier never assigns them an
origin other than 'ai' by design — those are Layer 3's job.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from conftest import Fixture, RUNS_DIR, run_pipeline, walk_pis_field
from helpers import _value_appears_in_text
import judge
import judge_cache


# Field origins that count as "classified" (excluded narrative 'ai' slots).
_CLASSIFIABLE_STATES = {"verified", "web_grounded", "inferred", "hallucinated"}


# ── Pass A ───────────────────────────────────────────────────────────────────

def test_web_grounded_fields_appear_in_web_context(fixture: Fixture):
    """Every field tagged 'web_grounded' must be grep-findable in web_context.

    Catches regressions in the classifier — if 'web_grounded' fields stop
    appearing in the web text, either the classifier is broken or the
    web_context.txt was rotated without re-running the pipeline.
    """
    pis_data = run_pipeline(fixture)
    web_lower = (pis_data.get("_web_context") or "").lower()
    if not web_lower:
        pytest.skip("no web_context captured for this fixture")

    failures: list[tuple[str, str]] = []

    # Header / warranty
    for path, origin in (pis_data.get("_field_origins") or {}).items():
        if origin != "web_grounded":
            continue
        value = walk_pis_field(pis_data, path)
        if not value:
            failures.append((path, "<empty>"))
            continue
        if not _value_appears_in_text(value, web_lower):
            failures.append((path, str(value)))

    # Specs (web_grounded if key OR value in web text)
    specs = pis_data.get("technical_specifications") or {}
    for spec_key, origin in (pis_data.get("_spec_origins") or {}).items():
        if origin != "web_grounded":
            continue
        spec_value = specs.get(spec_key)
        if not (
            _value_appears_in_text(spec_value, web_lower)
            or _value_appears_in_text(spec_key, web_lower)
        ):
            failures.append((f"spec:{spec_key}", str(spec_value)))

    if failures:
        details = "\n".join(f"  - {p} = {v!r}" for p, v in failures)
        pytest.fail(
            f"[{fixture.name}] {len(failures)} 'web_grounded' field(s) "
            f"not found in web_context:\n{details}",
            pytrace=False,
        )


# ── Pass B helpers ───────────────────────────────────────────────────────────

def _iter_hallucinated_fields(pis_data: dict):
    """Yield (label, value) for every field tagged 'hallucinated' that has a
    non-empty value. label is the dotted path or 'spec:<key>'."""
    for path, origin in (pis_data.get("_field_origins") or {}).items():
        if origin != "hallucinated":
            continue
        value = walk_pis_field(pis_data, path)
        if value:
            yield path, value

    specs = pis_data.get("technical_specifications") or {}
    for spec_key, origin in (pis_data.get("_spec_origins") or {}).items():
        if origin != "hallucinated":
            continue
        v = specs.get(spec_key)
        if v:
            yield f"spec:{spec_key}", v


def _count_classified(pis_data: dict) -> int:
    """Total fields with an origin in _CLASSIFIABLE_STATES — denominator for
    the hallucination rate."""
    n = 0
    for v in (pis_data.get("_field_origins") or {}).values():
        if v in _CLASSIFIABLE_STATES:
            n += 1
    for v in (pis_data.get("_spec_origins") or {}).values():
        if v in _CLASSIFIABLE_STATES:
            n += 1
    return n


def _run_rescue_for_fixture(fixture: Fixture, pis_data: dict) -> dict:
    """Run Haiku rescue across every hallucinated field. Cached per-fixture.

    Returns dict with counts + per-field verdicts."""
    cache_path = fixture.dir / "judge_layer2_cache.json"
    proforma_text = fixture.proforma_text
    web_context = pis_data.get("_web_context") or ""
    srcs_h = judge_cache.sources_hash(proforma_text, web_context)

    rescued: list[dict] = []
    confirmed: list[dict] = []

    for label, value in _iter_hallucinated_fields(pis_data):
        payload = {"field": label, "value": str(value)}

        def _call_haiku():
            return judge.judge_field_value(
                field_name=label,
                field_value=value,
                proforma_text=proforma_text,
                web_context=web_context,
            )

        verdict = judge_cache.judged_or_run(
            cache_path=cache_path,
            judge="haiku_field_rescue",
            srcs_hash=srcs_h,
            payload=payload,
            run_fn=_call_haiku,
        )

        record = {
            "field": label,
            "value": str(value),
            "verdict": verdict,
        }
        if verdict.get("supported"):
            rescued.append(record)
        else:
            confirmed.append(record)

    return {
        "rescued": rescued,
        "confirmed": confirmed,
        "n_hallucinated_grep": len(rescued) + len(confirmed),
        "n_rescued": len(rescued),
        "n_hallucinated_judged": len(confirmed),
    }


# ── Pass B per-fixture (soft — never fails a fixture in isolation) ──────────

@pytest.mark.needs_anthropic
def test_hallucination_rate_per_fixture(fixture: Fixture):
    """Soft test — records per-fixture rate, never fails individually.

    Fixtures with sparse proformas (e.g. invoice-only) will naturally show
    high hallucination rates. Hard gating happens at the suite level.
    """
    pis_data = run_pipeline(fixture)
    classified = _count_classified(pis_data)
    if classified == 0:
        pytest.skip("no classified fields (proforma + web both empty)")

    result = _run_rescue_for_fixture(fixture, pis_data)

    rate_grep = result["n_hallucinated_grep"] / classified
    rate_judged = result["n_hallucinated_judged"] / classified

    print(
        f"\n  [{fixture.name}] classified={classified} "
        f"hallucinated_grep={result['n_hallucinated_grep']} "
        f"(rate={rate_grep:.1%}) "
        f"rescued={result['n_rescued']} "
        f"final={result['n_hallucinated_judged']} "
        f"(rate={rate_judged:.1%})"
    )


# ── Aggregate summary (the hard gate) ───────────────────────────────────────

@pytest.mark.needs_anthropic
def test_layer2_summary(all_fixtures: list[Fixture]):
    """Aggregate report + hard fail if hallucination rate after rescue is
    above the threshold. Generous threshold on first runs; tighten as the
    baseline becomes known."""

    HALLUCINATION_THRESHOLD = 0.50   # generous for first runs

    summary = {
        "layer": 2,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": HALLUCINATION_THRESHOLD,
        "fixtures": [],
        "totals": {
            "classified_fields": 0,
            "hallucinated_grep": 0,
            "rescued": 0,
            "hallucinated_judged": 0,
        },
    }

    for f in all_fixtures:
        pis_data = run_pipeline(f)
        classified = _count_classified(pis_data)
        if classified == 0:
            summary["fixtures"].append({
                "name": f.name, "skipped": "no classified fields",
            })
            continue

        result = _run_rescue_for_fixture(f, pis_data)
        rate_grep = result["n_hallucinated_grep"] / classified
        rate_judged = result["n_hallucinated_judged"] / classified

        summary["fixtures"].append({
            "name": f.name,
            "classified": classified,
            "hallucinated_grep": result["n_hallucinated_grep"],
            "rescued": result["n_rescued"],
            "hallucinated_judged": result["n_hallucinated_judged"],
            "rate_grep": round(rate_grep, 4),
            "rate_judged": round(rate_judged, 4),
            "confirmed_hallucinations": [
                {"field": r["field"], "value": r["value"][:120]}
                for r in result["confirmed"]
            ],
        })
        summary["totals"]["classified_fields"] += classified
        summary["totals"]["hallucinated_grep"] += result["n_hallucinated_grep"]
        summary["totals"]["rescued"] += result["n_rescued"]
        summary["totals"]["hallucinated_judged"] += result["n_hallucinated_judged"]

    t = summary["totals"]
    overall_grep = t["hallucinated_grep"] / t["classified_fields"] if t["classified_fields"] else 0
    overall_judged = t["hallucinated_judged"] / t["classified_fields"] if t["classified_fields"] else 0
    summary["overall_rate_grep"] = round(overall_grep, 4)
    summary["overall_rate_judged"] = round(overall_judged, 4)
    summary["anthropic_cost"] = judge.get_call_log().summary()

    # Save report
    safe_ts = summary["timestamp"].replace(":", "-")
    report_path = RUNS_DIR / f"layer2_{safe_ts}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"\n[Layer 2] {len(all_fixtures)} fixtures | "
        f"classified={t['classified_fields']} | "
        f"hallucinated_grep={t['hallucinated_grep']} ({overall_grep:.1%}) | "
        f"rescued={t['rescued']} | "
        f"final={t['hallucinated_judged']} ({overall_judged:.1%})\n"
        f"  Anthropic spend this run: ${summary['anthropic_cost']['total_cost_usd']:.4f}\n"
        f"  Report: {report_path.relative_to(report_path.parents[2])}"
    )

    assert overall_judged <= HALLUCINATION_THRESHOLD, (
        f"Layer 2 hallucination rate after Haiku rescue is {overall_judged:.1%} "
        f"(threshold {HALLUCINATION_THRESHOLD:.0%}). See {report_path.name}."
    )
