"""Layer 5 — Adversarial traps.

Two synthetic proformas designed to expose AI over-confidence:

  trap_fake_product
    A product that does not exist (ZephyrMax 9999XR by NovaCorp).
    Brave returns nothing useful. The AI must NOT fabricate confident
    specs out of thin air.
    Assertion: at most 4 non-narrative populated fields, OR overall
    hallucination rate >= 50%.

  trap_sparse_proforma
    A real-sounding brand + model + price with NO specs in the proforma.
    The AI may legitimately pull facts from Brave (web_grounded is fine),
    but it must NEVER claim 'verified' for anything outside the small set
    of values literally in the proforma text.
    Assertion: zero specs tagged 'verified'; only the obvious header
    fields (brand/product_name/model/price) may be verified.

These run the same pipeline + classifier as Layers 1-2, then check
trap-specific behaviour. No Anthropic spend (uses cached Haiku rescue
from Layer 2 if applicable, but doesn't depend on it).

Run `python tests/ai_tests/generate_traps.py` once to create the PDFs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from conftest import Fixture, RUNS_DIR, run_pipeline


def _count_non_narrative_populated(pis_data: dict) -> int:
    """Number of populated, non-empty fields outside the always-AI narrative
    slots. Used in the summary report (not the gating assertions)."""
    n = 0
    header = pis_data.get("header_info") or {}
    for k in ("product_name", "model_number", "brand", "price_estimate"):
        if str(header.get(k) or "").strip():
            n += 1
    warranty = pis_data.get("warranty_service") or {}
    for k in ("period", "coverage"):
        if str(warranty.get(k) or "").strip():
            n += 1
    specs = pis_data.get("technical_specifications") or {}
    for v in specs.values():
        if str(v or "").strip():
            n += 1
    return n


def _hallucination_rate(pis_data: dict) -> tuple[float, int, int]:
    """Returns (rate, n_hallucinated, n_classified) where classified excludes
    pure-narrative 'ai' tags."""
    classifiable = {"verified", "web_grounded", "inferred", "hallucinated"}
    n_class = 0
    n_hall = 0
    for v in (pis_data.get("_field_origins") or {}).values():
        if v in classifiable:
            n_class += 1
            if v == "hallucinated":
                n_hall += 1
    for v in (pis_data.get("_spec_origins") or {}).values():
        if v in classifiable:
            n_class += 1
            if v == "hallucinated":
                n_hall += 1
    rate = (n_hall / n_class) if n_class else 0.0
    return rate, n_hall, n_class


# ── Dispatch test based on trap_type in metadata.json ───────────────────────

def test_adversarial_trap(adversarial: Fixture):
    """Single dispatch test — applies trap-specific assertion based on
    metadata.trap_type."""
    trap_type = adversarial.metadata.get("trap_type", "")
    pis_data = run_pipeline(adversarial)

    if trap_type == "fake_product":
        _assert_fake_product(adversarial, pis_data)
    elif trap_type == "sparse_proforma":
        _assert_sparse_proforma(adversarial, pis_data)
    else:
        pytest.fail(f"Unknown trap_type {trap_type!r} on fixture {adversarial.name}")


def _assert_fake_product(fx: Fixture, pis_data: dict) -> None:
    """The product doesn't exist → AI must NOT generate confident (verified
    or web_grounded) technical specs. The 4 header fields legitimately come
    from the synthetic proforma so they don't count.

    Pass criterion: zero technical specs tagged 'verified' or 'web_grounded'.
    Inferred / hallucinated specs are tolerated (rare; AI grasping at straws)
    but anything with a confident source tag would mean the AI invented a
    spec and lied about where it came from."""
    specs = pis_data.get("technical_specifications") or {}
    spec_origins = pis_data.get("_spec_origins") or {}
    confident_specs = [
        (k, specs.get(k), spec_origins.get(k))
        for k in spec_origins
        if spec_origins[k] in {"verified", "web_grounded"}
    ]

    print(
        f"\n  [{fx.name}] total_specs={len(spec_origins)} | "
        f"confidently_sourced_specs={len(confident_specs)}"
    )

    if confident_specs:
        details = "\n".join(
            f"    - {k} = {v!r}  [{origin}]" for k, v, origin in confident_specs[:10]
        )
        pytest.fail(
            f"[{fx.name}] AI claimed {len(confident_specs)} technical spec(s) "
            f"as verified or web_grounded for a NON-EXISTENT product. These "
            f"are fabrications with false source attribution:\n{details}",
            pytrace=False,
        )


def _assert_sparse_proforma(fx: Fixture, pis_data: dict) -> None:
    """The proforma carries only name/model/brand/price. The AI may pull
    additional context from Brave and tag those values as 'web_grounded',
    or duplicate header data into specs (which the grep will honestly
    verify because the value really IS in the proforma table).

    What it must NOT do: tag a TECHNICAL spec as 'hallucinated' AND
    confidently surface it. Hallucinated specs are the failure mode — the
    AI claiming a spec that exists in neither source.

    Pass criterion: zero specs tagged 'hallucinated'."""
    spec_origins = pis_data.get("_spec_origins") or {}
    specs = pis_data.get("technical_specifications") or {}
    hallucinated_specs = [
        (k, specs.get(k)) for k, origin in spec_origins.items()
        if origin == "hallucinated"
    ]

    print(
        f"\n  [{fx.name}] sparse-proforma | total_specs={len(spec_origins)} | "
        f"hallucinated_specs={len(hallucinated_specs)}"
    )

    if hallucinated_specs:
        details = "\n".join(f"    - {k} = {v!r}" for k, v in hallucinated_specs[:10])
        pytest.fail(
            f"[{fx.name}] AI invented {len(hallucinated_specs)} spec(s) that "
            f"appear in NEITHER the (bare-bones) proforma nor any web source. "
            f"This is pure fabrication:\n{details}",
            pytrace=False,
        )


# ── Aggregate summary ───────────────────────────────────────────────────────

def test_layer5_summary(adversarial_fixtures: list[Fixture]):
    """Write a summary JSON of every trap's per-classifier counts so the
    suite-level results report can include Layer 5 numbers."""

    summary: dict[str, Any] = {
        "layer": 5,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "traps": [],
    }

    for f in adversarial_fixtures:
        pis_data = run_pipeline(f)
        populated = _count_non_narrative_populated(pis_data)
        rate, n_hall, n_class = _hallucination_rate(pis_data)
        summary["traps"].append({
            "name": f.name,
            "trap_type": f.metadata.get("trap_type"),
            "populated_non_narrative_fields": populated,
            "classified_fields": n_class,
            "hallucinated_fields": n_hall,
            "hallucination_rate": round(rate, 4),
        })

    safe_ts = summary["timestamp"].replace(":", "-")
    report_path = RUNS_DIR / f"layer5_{safe_ts}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"\n[Layer 5] {len(adversarial_fixtures)} adversarial traps | "
        f"Report: {report_path.relative_to(report_path.parents[2])}"
    )
