# AI Accuracy Test Suite

Automated tests that measure how often the PIS wizard's AI output is grounded
in real sources vs. hallucinated. See [docs/TEST_PLAN.md](docs/TEST_PLAN.md)
for the full strategy and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
for the v1→v2 fix plan.

## Folder layout

```
tests/ai_tests/
├── README.md            ← you are here
├── conftest.py          ← pytest discovery, fixture model, pipeline runner
├── requirements.txt     ← anthropic + reportlab (dev-only deps)
├── .gitignore
│
├── tests/               ← the 5 layer test files
│   ├── test_layer1_facts.py
│   ├── test_layer2_web_grounded.py
│   ├── test_layer3_faithfulness.py
│   ├── test_layer4_consistency.py
│   └── test_layer5_adversarial.py
│
├── lib/                 ← shared judging helpers
│   ├── judge.py             ← Anthropic LLM-as-judge calls (Haiku + Sonnet)
│   └── judge_cache.py       ← on-disk cache so re-runs cost $0
│
├── tools/               ← one-shot scripts
│   ├── freeze_fixtures.py   ← capture Brave web_context per fixture
│   └── generate_traps.py    ← build the adversarial PDFs (Layer 5)
│
├── data/                ← what the tests READ
│   ├── fixtures/            ← 10 real proformas (one folder per fixture)
│   │   ├── single_poco_x7/
│   │   │     ├── proforma.pdf
│   │   │     ├── metadata.json
│   │   │     └── web_context.txt   (frozen by tools/freeze_fixtures.py)
│   │   └── …
│   └── adversarial/         ← 2 synthetic trap fixtures (Layer 5)
│       ├── trap_fake_product/
│       └── trap_sparse_proforma/
│
├── docs/                ← strategy + planning
│   ├── TEST_PLAN.md         ← full eval strategy
│   └── IMPLEMENTATION_PLAN.md   ← v1→v2 fix plan
│
└── results/             ← what the tests WRITE
    ├── runs/                ← timestamped reports from the latest pytest run
    ├── v1/                  ← archived v1 baseline (pre-fix)
    │   ├── results.md
    │   ├── runs/
    │   ├── fixtures/        (per-fixture caches as they were in v1)
    │   └── adversarial/
    └── v2/                  ← archived v2 (post-fix + OCR refresh)
        ├── results.md
        ├── v1_vs_v2_comparison.md
        ├── pytest_full_output.log
        ├── runs/
        ├── fixtures/
        └── adversarial/
```

## One-time setup

```bash
# 1. Install dev-only deps (anthropic SDK + reportlab for adversarial PDFs +
#    pytesseract for PNG OCR — see Fix #8 in the implementation plan).
pip install -r tests/ai_tests/requirements.txt

# 2. On Windows, also install the tesseract binary so OCR works:
#    winget install --id UB-Mannheim.TesseractOCR --exact
#    On Linux: apt-get install tesseract-ocr

# 3. Make sure these env vars are in .env at the project root:
#    GOOGLE_API_KEY=…
#    BRAVE_SEARCH_API_KEY=…
#    ANTHROPIC_API_KEY=…       (needed for Layer 2, 3, 5)

# 4. Freeze Brave web_context for every fixture (one-time cost, then $0)
python tests/ai_tests/tools/freeze_fixtures.py
```

After freezing, every pytest run uses the saved `web_context.txt` per fixture
— Brave is never hit again, deterministic + free.

## First-run cost (one-time)

The first `pytest` run pays for two things and caches the results to disk:

| Resource | Where it lands | Cost |
|---|---|---|
| Brave web context | `data/fixtures/<name>/web_context.txt` | $0 (free tier) |
| Gemini-generated `pis_data` | `data/fixtures/<name>/pis_data.json` | ~$0.20 across all 10 fixtures |
| Anthropic Haiku + Sonnet judgments | `data/fixtures/<name>/judge_layer*_cache.json` | ~$1.10 across all fixtures |

Every subsequent run reads from these files — no Gemini, no Brave, no
Anthropic, $0 total.

To **invalidate the cache** (e.g. after changing a generation prompt):

```bash
# Remove all cached pis_data
rm tests/ai_tests/data/fixtures/*/pis_data*.json

# Or one specific fixture
rm tests/ai_tests/data/fixtures/single_poco_x7/pis_data*.json

# Wipe all judge caches too (forces re-judgment with the new generation)
rm tests/ai_tests/data/fixtures/*/judge_*_cache.json

# Re-fetch Brave (rarely needed; web_context is stable for the test set)
python tests/ai_tests/tools/freeze_fixtures.py --refresh
```

## Running

```bash
# Full eval suite
pytest tests/ai_tests/ -v

# One layer at a time
pytest tests/ai_tests/tests/test_layer1_facts.py -v
pytest tests/ai_tests/tests/test_layer2_web_grounded.py -v
# …etc

# One fixture across all layers
pytest tests/ai_tests/ -v -k single_poco_x7
```

## What each test layer measures

| Layer | File | Cost | What |
|---|---|---|---|
| 1 | `tests/test_layer1_facts.py` | $0 | Every field tagged `verified` actually appears in the proforma raw text |
| 2 | `tests/test_layer2_web_grounded.py` | ~$0.05 | Every `web_grounded` field appears in `web_context.txt`; `hallucinated` fields get a Haiku paraphrase-rescue pass |
| 3 | `tests/test_layer3_faithfulness.py` | ~$1.10 | AI-written paragraphs split into atomic claims (Haiku) and each judged for support (Sonnet — now reads the PDF directly via document blocks) |
| 4 | `tests/test_layer4_consistency.py` | $0 Anthropic | Same fixture generated 3× → measure agreement on spec values |
| 5 | `tests/test_layer5_adversarial.py` | ~$0.05 | 2 synthetic trap PDFs: fake product + sparse proforma. Asserts AI doesn't fabricate. |

Approx total for one full run with empty caches: **~$1.20 Anthropic + ~$0.40 Gemini = ~$1.60**.

## Skip behaviour

Tests are auto-skipped if their required API key is missing:

- No `GOOGLE_API_KEY` → entire suite skipped (no generation possible)
- No `ANTHROPIC_API_KEY` → Layers 2, 3, 5 skipped; Layers 1 and 4 still run

## Adding a new fixture

1. Drop the proforma file into `data/fixtures/<name>/proforma.pdf` (or `.png`)
2. Create `data/fixtures/<name>/metadata.json`:
   ```json
   {
     "fixture": "<name>",
     "kind": "single",
     "product_name": "Brand Model Number",
     "brand": "Brand",
     "input_file": "proforma.pdf",
     "notes": "..."
   }
   ```
3. Run `python tests/ai_tests/tools/freeze_fixtures.py --only <name>` to capture Brave once
4. `pytest tests/ai_tests/ -k <name>` to test it
