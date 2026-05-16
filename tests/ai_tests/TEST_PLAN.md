# AI Content Accuracy Test Plan

Automated testing for the PIS AI content generation pipeline. No humans in the loop.

## What we test

The PIS wizard generates product data from:
1. **Proforma PDF/image** — should be treated as ground truth (facts)
2. **Brave-discovered supplier pages** — fetched live, treated as secondary source
3. **AI inference** — anything the LLM produces that isn't in either source

Every generated field falls into one of four origin tags:

| Tag | Meaning | Trust |
|---|---|---|
| `verified` | Value appears in the proforma raw text | Fact |
| `web_grounded` | Value appears in the frozen Brave web_context | Probably true |
| `inferred` | AI says it inferred this spec (no source check possible) | Unknown |
| `hallucinated` | Value appears in neither source | AI made it up |

The whole eval is built on counting and validating these tags.

## Prerequisite (Step 0 — must run before any test works)

Extend `helpers.classify_flat_pis_origins()` to produce the 4-state tagging instead of the current 3-state. Also persist `_web_context` onto pis_data so judges can re-use the exact source text the generator saw.

**Files touched:**
- `utils/bulk_wizard.py` — save web_context onto pis_data after gather call
- `utils/single_wizard.py` — same
- `helpers.py` — extend `classify_flat_pis_origins()` with the new logic

**New logic:**
```
if value in proforma_text:        verified
elif value in web_context:        web_grounded
elif came from inferred_specs:    inferred
else:                             hallucinated
```

Without this, the eval has nothing to measure.

## The 5 test layers

### Layer 1 — Facts grep (no LLM, runs on every PR)

For every field tagged `verified`, assert it really appears in the proforma raw text. Catches regressions in the verification logic itself.

**Pass criterion:** 100%
**Cost:** $0

### Layer 2 — Web-grounded check + Haiku rescue

**Pass A (grep, free):** every field tagged `web_grounded` must appear in `web_context.txt`.

**Pass B (Claude Haiku 4.5):** for every field tagged `hallucinated`, ask Haiku if the value is paraphrased anywhere in the sources. If yes, rescue it to `web_grounded`. Catches "8 kg" vs "8kg" vs "eight kilograms" cases the grep misses.

**Pass criterion:** `hallucination_rate_after_judge` <10%
**Cost:** ~$0.10 per run

### Layer 3 — Narrative faithfulness (Haiku + Sonnet, nightly)

For AI-written paragraphs (`range_overview`, `sales_arguments`, `seo_long_description`):

**Pass A (Claude Haiku):** split paragraph into atomic factual claims.
**Pass B (Claude Sonnet 4.6):** judge each claim against the sources.

`faithfulness = supported_claims / total_claims`

**Pass criterion:** ≥0.85
**Cost:** ~$0.25 per run

### Layer 4 — Consistency (no LLM cost, nightly)

Run the same fixture through generation 3 times. Compare spec values across runs.

`consistency = mean(most_common_value_count / 3) across all spec keys`

High disagreement = AI is guessing. Tracks prompt-induced ambiguity.

**Pass criterion:** ≥0.85
**Cost:** $0 Anthropic (3× Gemini generation cost)

### Layer 5 — Adversarial traps (reuses Layers 1–3)

Two synthetic proformas that should break a poorly-prompted AI:

| Trap | Assertion |
|---|---|
| `trap_fake_product` | Specs mostly empty OR ≥80% tagged `hallucinated` |
| `trap_sparse_proforma` | Zero fields tagged `verified`; everything must be `web_grounded` / `inferred` / empty |

## Fixtures (10 real + 2 traps)

```
tests/ai_tests/
  fixtures/
    single_ariete_1/         (PDF, simple appliance)
    single_poco_x7/          (PDF, dense spec phone)
    single_poco_m7/          (PDF, second phone — consistency baseline)
    single_belair_freezers/  (PDF, in brand library)
    single_sunon_png/        (PNG, image-only OCR test)
    single_xiaomi_png/       (PNG, image-only OCR test)
    bulk_ariete/             (multi-product PDF)
    bulk_belair_freezers/    (multi-product + brand library)
    bulk_sunon_wardrobes/    (sparse-spec furniture)
    bulk_xiaomi_tv/          (multi-product TVs)
  adversarial/
    trap_fake_product/       (generated PDF — non-existent product)
    trap_sparse_proforma/    (generated PDF — name + price only)
```

Each fixture directory contains:
- `proforma.pdf` (or `.png`) — the input
- `web_context.txt` — frozen Brave output, captured once on first run
- `expected.yaml` — minimum assertions (e.g. brand must be "Ariete", ≥5 specs)

## Judges — which Claude model where

| Model | Purpose | Why |
|---|---|---|
| **Claude Haiku 4.5** | Claim extraction (Layer 3 Pass A); paraphrase rescue (Layer 2 Pass B) | Cheap, fast, structural tasks |
| **Claude Sonnet 4.6** | Claim verification (Layer 3 Pass B) | Strong NLI reasoning for the high-stakes judgment |

**No Opus.** Sonnet is the ceiling.

**Cross-family bias protection:** Generator is Gemini → judges are Claude. Same-family judges over-approve their own model's output.

## Judge prompts (the exact wording)

### Haiku — paraphrase rescue (Layer 2 Pass B)

```
You verify whether a single product field value is supported by source documents.

SOURCES (the only allowed evidence):
=== PROFORMA TEXT ===
<proforma_text>

=== SUPPLIER WEB PAGES ===
<web_context>

CLAIM:
Field name: <field_name>
Field value: <field_value>

RULES:
- Paraphrases, unit conversions, abbreviations, and obvious synonyms count as supported.
  Examples: "8 kg" ≈ "8kg" ≈ "eight kilograms"; "Bluetooth 5.0" ≈ "BT 5.0"; "Stainless steel" ≈ "S/S".
- A claim is supported if EITHER source contains it. You do not need both.
- Inventions, plausible-sounding additions, marketing fluff with no source phrase = unsupported.
- If the value is empty or "N/A", reply unsupported.
- Be strict but not pedantic. When uncertain, mark unsupported.

Reply ONLY with valid JSON, no prose:
{
  "supported": true|false,
  "source": "proforma" | "web" | "neither",
  "evidence": "<exact phrase from source, ≤150 chars>"
}
```

### Haiku — claim extractor (Layer 3 Pass A)

```
Decompose this product description into atomic factual claims about the product.

Rules:
- One fact per claim.
- Each claim must be self-contained — replace pronouns with the product name.
- Skip purely subjective/marketing phrases ("the best", "amazing design") UNLESS they contain a specific fact.
- Keep each claim short (≤25 words).

Product name: <product_name>

Description:
<narrative_text>

Reply ONLY with valid JSON:
{
  "claims": [
    "<claim 1>",
    "<claim 2>"
  ]
}
```

### Sonnet — claim verifier (Layer 3 Pass B)

```
You judge whether a factual claim about a product is supported by the provided source documents. You must be strict.

SOURCES (the only allowed evidence):
=== PROFORMA TEXT ===
<proforma_text>

=== SUPPLIER WEB PAGES ===
<web_context>

CLAIM:
<single atomic claim>

DEFINITION OF "SUPPORTED":
- The claim can be directly read or trivially paraphrased from the sources.
- Unit conversions, synonyms, and standard abbreviations are fine.
- The claim does NOT add new facts beyond what the sources say.

DEFINITION OF "NOT SUPPORTED":
- The claim adds a fact, number, feature, or detail not in either source.
- The claim is generic marketing language ("high quality", "innovative", "energy efficient") without a specific spec in the sources backing it.
- The claim is a reasonable-sounding inference that goes beyond the literal sources.
- You cannot find an exact phrase in either source that matches the claim.

PROCEDURE:
1. Find the strongest supporting phrase in either source (if any).
2. Compare it to the claim word-by-word.
3. If anything in the claim is NOT covered by that phrase, mark unsupported.

Reply ONLY with valid JSON:
{
  "supported": true|false,
  "evidence": "<exact source phrase, ≤200 chars, or empty if unsupported>",
  "reason": "<one short sentence>"
}
```

All judge calls use:
- `temperature=0` (deterministic verdicts)
- Anthropic **prompt caching** on the sources block (saves ~80% tokens on repeated claims for the same product)

## Directory layout

Everything related to AI accuracy testing lives under `tests/ai_tests/`:

```
tests/ai_tests/
  TEST_PLAN.md             (this file)
  README.md                (how to run)
  conftest.py              (fixture loader, Brave monkeypatch)
  judge.py                 (Anthropic SDK wrapper, prompt caching, retries)
  fixtures/                (10 real proformas + frozen web_context)
  adversarial/             (2 synthetic traps)
  test_layer1_facts.py
  test_layer2_web_grounded.py
  test_layer3_faithfulness.py
  test_layer4_consistency.py
  test_layer5_adversarial.py
  runs/                    (timestamped JSON score reports)
```

## What a full run looks like

```
$ pytest tests/ai_tests/ -v

[Layer 1] facts grounded:         100% (47/47)
[Layer 2] hallucination_rate:       6% (after Haiku rescue)
[Layer 3] narrative faithfulness: 0.89
[Layer 4] consistency (3× runs):  0.87
[Layer 5] adversarial pass:       2/2 traps

Total cost: ~$0.41
Time: ~4 min
Report: tests/ai_tests/runs/2026-05-16_abc123.json
```

## Cost budget

| Layer | Per-run cost | Frequency | Monthly |
|---|---|---|---|
| 1 | $0 | every PR | $0 |
| 2 | ~$0.10 | every PR | ~$3 |
| 3 | ~$0.25 | nightly | ~$7.50 |
| 4 | $0 Anthropic | nightly | $0 |
| 5 | ~$0.05 | every PR | ~$1.50 |

**~$12/month total.** Spend cap on console.anthropic.com set to $30/mo for safety.

## Build order

1. Step 0 — extend `classify_flat_pis_origins` with `web_grounded` + `hallucinated`
2. Step 1 — fixture directory + `conftest.py` (Brave monkeypatch) + `judge.py`
3. Step 2 — Layer 1 test (cheapest, validates the tags are even being produced)
4. Step 3 — Layer 2 test (first LLM judge call — proves judge.py works end-to-end)
5. Step 4 — Layer 3 test (the expensive one, only after Layer 2 is green)
6. Step 5 — Layer 4 consistency test
7. Step 6 — Generate the 2 adversarial PDFs + Layer 5 test

Each step is a separate commit. We don't move forward until the previous one is green.
