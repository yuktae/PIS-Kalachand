"""Layer 1 — Facts grep.

For every field tagged `verified` by classify_flat_pis_origins, assert the
value really appears in the proforma raw text. Catches regressions in the
verification logic itself: if anyone tweaks _value_appears_in_text or the
classifier in a way that lets non-grounded values slip past as "verified",
this test fails.

No LLM cost after first freeze — pis_data is cached per fixture in
fixtures/<name>/pis_data.json after the first run.

First-time cost: ~$0.20 in Gemini to generate all 10 fixtures' pis_data.
After that: pure grep, $0 forever (until you set force_refresh=True).
"""

from __future__ import annotations

import pytest

from conftest import Fixture, run_pipeline, walk_pis_field
from helpers import _value_appears_in_text


def _collect_verified_failures(pis_data: dict, proforma_text: str) -> list[tuple[str, str]]:
    """Return [(field_path, value), ...] for every 'verified' tag whose value
    cannot be grep-matched in the proforma raw text."""
    raw_lower = (proforma_text or "").lower()
    failures: list[tuple[str, str]] = []

    # ── Header / warranty fields ──
    for path, origin in (pis_data.get("_field_origins") or {}).items():
        if origin != "verified":
            continue
        value = walk_pis_field(pis_data, path)
        if not value:
            # Empty value can't logically be 'verified' — flag it.
            failures.append((path, "<empty>"))
            continue
        if not _value_appears_in_text(value, raw_lower):
            failures.append((path, str(value)))

    # ── Technical specs (verified = key OR value appears) ──
    specs = pis_data.get("technical_specifications") or {}
    for spec_key, origin in (pis_data.get("_spec_origins") or {}).items():
        if origin != "verified":
            continue
        spec_value = specs.get(spec_key)
        appears = (
            _value_appears_in_text(spec_value, raw_lower)
            or _value_appears_in_text(spec_key, raw_lower)
        )
        if not appears:
            failures.append((f"spec:{spec_key}", str(spec_value)))

    return failures


def test_verified_fields_appear_in_proforma(fixture: Fixture):
    """Every field tagged 'verified' must be grep-findable in the proforma."""
    pis_data = run_pipeline(fixture)
    proforma_text = fixture.proforma_text

    failures = _collect_verified_failures(pis_data, proforma_text)

    if failures:
        details = "\n".join(f"  - {path} = {value!r}" for path, value in failures)
        pytest.fail(
            f"[{fixture.name}] {len(failures)} 'verified' field(s) "
            f"not found in proforma raw text:\n{details}",
            pytrace=False,
        )


def test_verified_count_is_meaningful(fixture: Fixture):
    """Sanity check: a fixture with extractable proforma text should produce
    at least one 'verified' field. Pure-image PNGs are exempt — they have no
    extractable text so nothing can be grep-verified by definition."""
    proforma_text = fixture.proforma_text
    if not proforma_text.strip():
        pytest.skip("no extractable proforma text (image-only fixture)")

    pis_data = run_pipeline(fixture)
    field_origins = pis_data.get("_field_origins") or {}
    spec_origins = pis_data.get("_spec_origins") or {}

    n_verified = (
        sum(1 for v in field_origins.values() if v == "verified")
        + sum(1 for v in spec_origins.values() if v == "verified")
    )
    assert n_verified > 0, (
        f"[{fixture.name}] zero 'verified' fields despite "
        f"{len(proforma_text)} chars of proforma text — "
        f"either the AI failed to extract any grounded value, or the "
        f"grep is broken."
    )


def test_layer1_summary(all_fixtures: list[Fixture]):
    """Aggregate report — printed at the end, written to runs/layer1_*.json."""
    import json
    from datetime import datetime, timezone

    summary = {
        "layer": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fixtures": [],
        "totals": {"verified_fields": 0, "verified_specs": 0, "failures": 0},
    }

    for f in all_fixtures:
        pis_data = run_pipeline(f)
        failures = _collect_verified_failures(pis_data, f.proforma_text)
        field_origins = pis_data.get("_field_origins") or {}
        spec_origins = pis_data.get("_spec_origins") or {}
        n_v_fields = sum(1 for v in field_origins.values() if v == "verified")
        n_v_specs = sum(1 for v in spec_origins.values() if v == "verified")

        summary["fixtures"].append({
            "name": f.name,
            "verified_fields": n_v_fields,
            "verified_specs": n_v_specs,
            "failures": len(failures),
            "failed_items": [f"{p}={v}" for p, v in failures],
        })
        summary["totals"]["verified_fields"] += n_v_fields
        summary["totals"]["verified_specs"] += n_v_specs
        summary["totals"]["failures"] += len(failures)

    # Write report
    from conftest import RUNS_DIR
    report_path = RUNS_DIR / f"layer1_{summary['timestamp'].replace(':', '-')}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    total_verified = summary["totals"]["verified_fields"] + summary["totals"]["verified_specs"]
    accuracy = (
        1.0 - (summary["totals"]["failures"] / total_verified)
        if total_verified else 1.0
    )
    print(
        f"\n[Layer 1] {len(all_fixtures)} fixtures | "
        f"{total_verified} verified | {summary['totals']['failures']} failed | "
        f"accuracy={accuracy:.1%}\n"
        f"  report: {report_path.relative_to(report_path.parents[2])}"
    )

    # Hard gate: 100% required. If this ever drops below 100%, the per-fixture
    # tests above will already have failed — this is just the headline number.
    assert summary["totals"]["failures"] == 0, (
        f"Layer 1 hallucinated 'verified' tags: {summary['totals']['failures']} "
        f"failures across {len(all_fixtures)} fixtures."
    )
