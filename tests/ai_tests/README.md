# AI Accuracy Test Suite

Automated tests that measure how often the PIS wizard's AI output is grounded
in real sources vs. hallucinated. See [TEST_PLAN.md](TEST_PLAN.md) for the
full strategy.

## One-time setup

```bash
# 1. Install dev-only deps (anthropic SDK + reportlab for adversarial PDFs)
pip install -r tests/ai_tests/requirements.txt

# 2. Make sure these env vars are in .env at the project root:
#    GEMINI_API_KEY=...            (already there)
#    BRAVE_SEARCH_API_KEY=...      (already there)
#    ANTHROPIC_API_KEY=...         (needed for Layer 2, 3, 5)

# 3. Freeze Brave web_context for every fixture (one-time cost, then free)
python tests/ai_tests/freeze_fixtures.py
```

After freezing, every test run uses the saved `web_context.txt` per fixture —
Brave is never hit again. Deterministic + free.

## First-run cost (one-time)

The first `pytest` run pays for two things and caches the results to disk:

| Resource | Where it's saved | Cost |
|---|---|---|
| Brave web context | `fixtures/<name>/web_context.txt` | Free (Brave free tier) |
| Gemini-generated `pis_data` | `fixtures/<name>/pis_data.json` | ~$0.20 across all 10 fixtures |

Every subsequent run reads from these files — no Gemini, no Brave, $0.

To **invalidate the cache** (e.g. after changing a generation prompt):

```bash
# Remove all cached pis_data
rm tests/ai_tests/fixtures/*/pis_data.json

# Or one specific fixture
rm tests/ai_tests/fixtures/single_poco_x7/pis_data.json

# Re-fetch Brave (rarely needed; web_context is stable for the test set)
python tests/ai_tests/freeze_fixtures.py --refresh
```

## Running

```bash
# Full eval suite
pytest tests/ai_tests/ -v

# One layer at a time
pytest tests/ai_tests/test_layer1_facts.py -v
pytest tests/ai_tests/test_layer2_web_grounded.py -v
# ...etc

# One fixture
pytest tests/ai_tests/ -v -k single_poco_x7
```

## What each test layer measures

| Layer | File | Cost | What |
|---|---|---|---|
| 1 | `test_layer1_facts.py` | $0 | Every field tagged `verified` actually appears in the proforma raw text |
| 2 | `test_layer2_web_grounded.py` | ~$0.10 | Every `web_grounded` field appears in `web_context.txt`; `hallucinated` fields get a Haiku paraphrase-rescue pass |
| 3 | `test_layer3_faithfulness.py` | ~$0.80 | AI-written paragraphs split into atomic claims (Haiku) and each judged for support (Sonnet) |
| 4 | `test_layer4_consistency.py` | $0 Anthropic | Same fixture generated 3× → measure agreement on spec values |
| 5 | `test_layer5_adversarial.py` | ~$0.05 | 2 synthetic trap PDFs: fake product + sparse proforma. Asserts AI doesn't fabricate. |

Approx total for one full run: **~$1.00 – $1.50** in Anthropic, **~$0.25** in Gemini.

## Directory layout

```
tests/ai_tests/
  TEST_PLAN.md             ← full strategy
  README.md                ← you are here
  requirements.txt         ← anthropic + reportlab
  conftest.py              ← fixture loader, pipeline runner, Brave freezing
  judge.py                 ← Anthropic LLM-as-judge helpers
  freeze_fixtures.py       ← one-time Brave capture script
  fixtures/                ← 10 real proformas
    single_ariete_1/
      proforma.pdf
      metadata.json
      web_context.txt      ← frozen on first run
    ...
  adversarial/             ← 2 synthetic trap fixtures (built in Step 6)
  runs/                    ← timestamped JSON score reports
  test_layer1_facts.py     ← built in Step 2
  test_layer2_web_grounded.py  ← built in Step 3
  ...
```

## Adding a new fixture

1. Drop the proforma file into `fixtures/<name>/proforma.pdf` (or `.png`)
2. Create `fixtures/<name>/metadata.json`:
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
3. Run `python tests/ai_tests/freeze_fixtures.py --only <name>` to capture Brave once
4. `pytest tests/ai_tests/ -k <name>` to test it

## Skip behaviour

Tests are auto-skipped if their required API key is missing:

- No `GEMINI_API_KEY` → entire suite skipped (no generation possible)
- No `ANTHROPIC_API_KEY` → Layers 2, 3, 5 skipped; Layers 1 and 4 still run
