"""Layer 4 — Consistency.

Runs each fixture through the wizard 3 times with the same input and
measures whether the AI gives the same answers. High disagreement on the
same input means the model is guessing — a strong hallucination signal
that requires no ground truth.

Scoring per fixture:

  Header consistency
    For each of (product_name, model_number, brand, price_estimate):
      score = count_of_most_common_normalised_value / 3
    header_consistency = mean of the 4 scores

  Spec consistency
    Union of all spec keys across the 3 runs.
    For each key:
      - Count appearances (1, 2, or 3)
      - Count copies of the most-common normalised value among the appearances
      - score = max_value_count / 3
        (penalises both missing keys AND disagreeing values)
    spec_consistency = mean of per-key scores

  Overall = mean(header_consistency, spec_consistency)

Cost: ~$0.10 in Gemini for the 2 extra runs per fixture (run 1 reuses
the cached pis_data.json from Layer 1). $0 in Anthropic.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import pytest

from conftest import Fixture, RUNS_DIR, run_pipeline


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(v: Any) -> str:
    """Canonical form for value comparison — case-insensitive, collapsed
    whitespace, stripped punctuation tails so 'Rs 5,000.' == 'Rs 5,000'."""
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(".,;:")
    return s


def _agreement_score(values: list[str], n_runs: int) -> float:
    """Most-common value count divided by total runs. Penalises both missing
    values (treated as ""), and disagreements."""
    # Filter out empties (treat missing as a distinct vote, not the winner)
    counts = Counter(values)
    counts.pop("", None)
    if not counts:
        return 0.0
    return counts.most_common(1)[0][1] / n_runs


def _three_runs(fixture: Fixture) -> list[dict]:
    """Return the fixture's 3 cached pis_data runs, generating any that
    don't yet exist."""
    return [
        run_pipeline(fixture, cache_filename=name)
        for name in (
            "pis_data.json",        # run 1 = same cache Layer 1 populated
            "pis_data_run2.json",
            "pis_data_run3.json",
        )
    ]


HEADER_FIELDS = ("product_name", "model_number", "brand", "price_estimate")


def _score_header(runs: list[dict]) -> dict:
    per_field: dict[str, float] = {}
    raw: dict[str, list[str]] = {}
    for f in HEADER_FIELDS:
        vals = [_normalize((r.get("header_info") or {}).get(f)) for r in runs]
        per_field[f] = _agreement_score(vals, len(runs))
        raw[f] = vals
    return {
        "per_field": per_field,
        "raw_values": raw,
        "consistency": (
            sum(per_field.values()) / len(per_field) if per_field else 1.0
        ),
    }


def _score_specs(runs: list[dict]) -> dict:
    """Union all spec keys across runs, score each key's agreement."""
    n_runs = len(runs)
    specs_per_run = [
        (r.get("technical_specifications") or {}) if isinstance(r, dict) else {}
        for r in runs
    ]

    # Normalize spec keys too — AI sometimes flips between "Power" and "power"
    def _norm_key(k: str) -> str:
        return _normalize(k)

    all_keys: set[str] = set()
    for sp in specs_per_run:
        all_keys.update(_norm_key(k) for k in sp.keys())

    per_key: dict[str, float] = {}
    sample: dict[str, list[str]] = {}
    for k in sorted(all_keys):
        values: list[str] = []
        for sp in specs_per_run:
            # Find a key matching k under normalization
            match = next(
                (orig for orig in sp.keys() if _norm_key(orig) == k),
                None,
            )
            values.append(_normalize(sp.get(match)) if match is not None else "")
        per_key[k] = _agreement_score(values, n_runs)
        sample[k] = values

    return {
        "n_unique_keys": len(all_keys),
        "per_key": per_key,
        "raw_values": sample,
        "consistency": (
            sum(per_key.values()) / len(per_key) if per_key else 1.0
        ),
    }


def _score_fixture(fixture: Fixture) -> dict:
    runs = _three_runs(fixture)
    header = _score_header(runs)
    specs = _score_specs(runs)
    overall = (header["consistency"] + specs["consistency"]) / 2
    return {
        "name": fixture.name,
        "n_runs": len(runs),
        "header": header,
        "specs": specs,
        "overall_consistency": overall,
    }


# ── Per-fixture soft test ───────────────────────────────────────────────────

def test_consistency_per_fixture(fixture: Fixture):
    """Soft per-fixture record. Aggregate is the gate."""
    result = _score_fixture(fixture)

    # Inline highlights of any disagreement
    disagreeing_headers = [
        f for f, score in result["header"]["per_field"].items() if score < 1.0
    ]
    n_disagreeing_specs = sum(
        1 for s in result["specs"]["per_key"].values() if s < 1.0
    )

    print(
        f"\n  [{fixture.name}] header={result['header']['consistency']:.0%} "
        f"specs={result['specs']['consistency']:.0%} "
        f"overall={result['overall_consistency']:.0%} "
        f"(header disagreements: {disagreeing_headers or 'none'}, "
        f"spec disagreements: {n_disagreeing_specs}/{result['specs']['n_unique_keys']})"
    )


# ── Aggregate summary (the hard gate) ───────────────────────────────────────

def test_layer4_summary(all_fixtures: list[Fixture]):
    """Aggregate consistency across every fixture. Generous threshold on
    first runs — tighten once baseline is known."""

    CONSISTENCY_THRESHOLD = 0.50

    summary: dict[str, Any] = {
        "layer": 4,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": CONSISTENCY_THRESHOLD,
        "fixtures": [],
        "totals": {
            "n_fixtures": 0,
            "mean_header_consistency": 0.0,
            "mean_spec_consistency": 0.0,
            "mean_overall_consistency": 0.0,
        },
    }

    header_scores: list[float] = []
    spec_scores: list[float] = []
    overall_scores: list[float] = []

    for f in all_fixtures:
        result = _score_fixture(f)
        summary["fixtures"].append({
            "name": result["name"],
            "header_consistency": round(result["header"]["consistency"], 4),
            "header_per_field": {
                k: round(v, 4) for k, v in result["header"]["per_field"].items()
            },
            "header_raw_values": result["header"]["raw_values"],
            "spec_consistency": round(result["specs"]["consistency"], 4),
            "n_unique_specs": result["specs"]["n_unique_keys"],
            "overall_consistency": round(result["overall_consistency"], 4),
            "low_agreement_specs": {
                k: result["specs"]["raw_values"][k]
                for k, score in result["specs"]["per_key"].items()
                if score < 0.67
            },
        })
        header_scores.append(result["header"]["consistency"])
        spec_scores.append(result["specs"]["consistency"])
        overall_scores.append(result["overall_consistency"])

    n = max(len(overall_scores), 1)
    summary["totals"]["n_fixtures"] = len(overall_scores)
    summary["totals"]["mean_header_consistency"] = round(
        sum(header_scores) / n, 4
    )
    summary["totals"]["mean_spec_consistency"] = round(
        sum(spec_scores) / n, 4
    )
    summary["totals"]["mean_overall_consistency"] = round(
        sum(overall_scores) / n, 4
    )

    safe_ts = summary["timestamp"].replace(":", "-")
    report_path = RUNS_DIR / f"layer4_{safe_ts}.json"
    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    t = summary["totals"]
    print(
        f"\n[Layer 4] {len(all_fixtures)} fixtures, 3 runs each | "
        f"header={t['mean_header_consistency']:.0%} | "
        f"specs={t['mean_spec_consistency']:.0%} | "
        f"overall={t['mean_overall_consistency']:.0%}\n"
        f"  Report: {report_path.relative_to(report_path.parents[2])}"
    )

    assert t["mean_overall_consistency"] >= CONSISTENCY_THRESHOLD, (
        f"Layer 4 consistency is {t['mean_overall_consistency']:.1%} "
        f"(threshold {CONSISTENCY_THRESHOLD:.0%}). See {report_path.name}."
    )
