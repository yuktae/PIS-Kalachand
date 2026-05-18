# PIS Wizard — AI Accuracy Fix Plan

---

## Phase 1 — Quick wins (same day, zero risk)

These are single-line changes with no side effects. Do them first, re-run the test suite, and the numbers will improve immediately.

- [ ] **Fix #1 — Pin the temperature**
  - **Issue:** The AI generates different specs from the same proforma every time it runs. Spec consistency is only 51% — the same product can look different depending on which generation got saved.
  - **Solution:** Add `temperature=0.1` to the two Gemini generation calls in `ai_generation.py`. This tells the model to be consistent rather than creative when extracting facts.

- [ ] **Fix #2 — Remove "Unavailable" placeholder strings**
  - **Issue:** When a field is absent (no price, no warranty), the AI writes `"Unavailable from document"`. That phrase is never in any source, so the verification system flags it as a hallucination — inflating the rate from ~6% to 11%.
  - **Solution:** Change the prompt instruction so that missing fields become an empty string `""` instead. No data is lost; the field just stays blank.

- [ ] **Fix #6 — Stop generating narratives when there is no source**
  - **Issue:** For PNG proformas where no text was extracted and the web search returned irrelevant pages, the AI writes full marketing paragraphs from general product knowledge. These score 0% on faithfulness because they cite nothing.
  - **Solution:** Add one line to the generation prompt: *"If no source text was extracted from the document and no relevant web context is available, leave all narrative fields blank."*

- [ ] **Fix #7 — Extract model number verbatim**
  - **Issue:** On sparse proformas (furniture, generic items), the AI sometimes rephrases or shortens the model number across runs, making it unstable.
  - **Solution:** Add to the extraction prompt: *"Copy model_number exactly as it appears in the document — no rephrasing, no abbreviation."*

---

## Phase 2 — Web search quality (1–2 hours)

Fixes the root cause of why PNG fixtures and some brand searches get irrelevant web pages.

- [ ] **Fix #3 — Smarter Brave search query**
  - **Issue:** The Brave search query is built from the brand name only (e.g. `"Xiaomi"`). For large brands with many product lines, this returns generic catalogue pages — monitors, earbuds, TVs all mixed together — instead of the specific model page. The AI then has no useful web context and either invents specs or tags everything as unverified.
  - **Solution 1:** Append the product category and model to the query (e.g. `"Xiaomi TV A Pro 32 specifications"` instead of `"Xiaomi"`).
  - **Solution 2:** Add a relevance check after fetching — if the returned page text does not contain the model number or core product terms, discard it rather than passing irrelevant content to the AI.

---

## Phase 3 — UI transparency (half day)

Makes the distinction between sourced facts and AI-deduced content visible to the marketing team.

- [ ] **Fix #5 — Clearer tagging for niche brand deductions**
  - **Issue:** For lesser-known brands (BelAir, Sunon, Kenstar), the system intentionally asks the AI to fill in typical specs when the proforma is too sparse. This is a deliberate and useful feature. The problem is that these deduced values are not always visually distinct from verified facts in the UI — the marketing team can't tell which specs came from the document and which were inferred.
  - **Solution:** Keep the `BRAND_CONTEXT_LIBRARY` exactly as-is. Just ensure that all deduced values are tagged `ai_enriched` in `_spec_origins`, and add a visible indicator in the PIS wizard UI (e.g. a different colour pill or a small label) so reviewers know which fields were inferred vs. sourced.

---

## Phase 4 — Test accuracy (half day)

Fixes the test framework so it correctly evaluates content — not a production change, but it will reveal that the actual AI quality is higher than the current scores suggest.

- [ ] **Fix #4 — L3 judge reads PDF tables properly**
  - **Issue:** The faithfulness judge (Layer 3) checks AI-written claims against plain text extracted from the proforma. But plain text extraction does not preserve table structure or merged cells. As a result, the judge wrongly flagged several correct features on the Ariete proformas as "unverified" — they were in a spec table the extractor couldn't read.
  - **Solution:** Pass the proforma to the judge as a file (the same way Gemini receives it), so the judge can see the actual table layout. Alternatively, use a table-aware PDF parser for the extraction step.

---

## Phase 5 — Long-term (1 day, optional)

Low urgency — only relevant if PNG proformas become a regular input type.

- [ ] **Fix #8 — OCR for PNG proformas**
  - **Issue:** Gemini already reads PNG images correctly — this is not an extraction problem. The issue is purely in the tagging layer: because no text is extracted from PNGs, the verification system cannot confirm any value as `verified`, so every correctly-read field shows as a red AI pill in the UI instead of a yellow checkmark. The test scores for PNG fixtures are also meaningless for the same reason.
  - **Solution:** Add an OCR pre-processing step (pytesseract or equivalent) that runs on PNG files before the verification pass. The extracted text feeds into the same grep-check that PDF text already goes through. Nothing changes in how Gemini reads the image — only the post-generation tagging becomes accurate.
