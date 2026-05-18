# v1 vs v2 — Side-by-Side Comparison (post-OCR refresh)

## Overall verdict

| Health metric | v1 (before fixes) | v2 (all 8 fixes + OCR) | Status |
|---|---|---|---|
| Classifier correctness | 100% | 100% | ✅ held |
| Hallucination rate | 11.2% | **0.6%** | ✅ **18× better** |
| Narrative truthfulness | 72.6% | **93.1%** | ✅ **+20.5 points** |
| Output stability — specs | 51% | **55%** | ✅ +4 points |
| Output stability — headers | 81% | 68% | 🟡 dragged down by empty-response runs + OCR noise |
| Adversarial restraint | 2/2 passed (with placeholders) | **2/2, zero hallucinations** | ✅ cleaner pass |

## AI Accuracy Tests — v1 vs v2 summary table

| Layer | What we did (plain English) | v1 result | v2 result | What changed |
|---|---|---|---|---|
| **0 — Foundation (UI tags)** | Added coloured badges in the wizard so the marketing team can see at a glance which fields came from the document, which came from the web, and which the AI made up. | 3 badge tiers (Fact / Web / AI) | **4 badge tiers** — added a purple "AI-enriched" pill for niche-brand deductions (BelAir, Sunon, etc.) | Reviewers can now tell apart "AI invented this" from "AI deduced this from typical brand knowledge" — two different trust levels that used to look identical. |
| **1 — Facts check** | For every value the AI labels as a fact, we double-check that the exact value actually appears on the proforma page. | 125/125 → **100% accuracy, 0 false claims** | 118/118 → **100% accuracy, 0 false claims** | Slightly fewer facts overall because the new AI is more cautious — when a value isn't printed, it leaves the field blank. PNG fixtures, which used to have *zero* verifiable facts, now have **5 verified facts each** thanks to the OCR step reading the image into text. |
| **2 — Hallucination check** | Counted how often the AI invents values that aren't in the proforma or supplier website. | 23 confirmed hallucinations → **11.2% rate** (about half were polite "Unavailable" placeholders) | **1 confirmed hallucination** → **0.6% rate** | Two changes combined: (a) we told the AI to leave unknown fields blank instead of writing "Unavailable", and (b) we now OCR the PNG proformas before checking, so values the AI read off the image (model number, price) can finally be verified. The Xiaomi PNG went from **90% hallucination rate to 0%**. |
| **3 — Story faithfulness** | Took every paragraph the AI wrote (product description, marketing copy, SEO) and checked each sentence against the source. | 305/420 claims supported → **72.6% faithfulness** | 283/304 claims supported → **93.1% faithfulness** | Three changes combined: (a) the AI stays silent when it has no source material, (b) the fact-checker now reads the PDF directly so it can see tables, and (c) OCR text from PNGs gives the checker something to verify against. The Sunon PNG fixture went from **0% to 97.1% faithfulness**. The Ariete fixture went from 73% to a perfect 100%. |
| **4 — Consistency (run 3×)** | Ran each proforma through the AI three times and checked whether it gave the same answer each time. | Headers 81% / Specs 51% / **Overall 66%** | Headers 68% / Specs **55%** / Overall 62% | Specs got more consistent as planned (Fix #1 worked). Headers regressed for two reasons: (i) on three dense PDFs Gemini occasionally returns a blank response, which zeros every header for that run; (ii) OCR text is slightly noisy, so the PNG fixtures' headers vary a bit between runs. Net trade-off: small consistency loss in exchange for massive faithfulness and hallucination wins. |
| **5 — Trap test** | Fed the AI two trap documents: a fake product, and a real-looking product with almost no info — to see if it would invent specs. | Both traps passed but produced "Unavailable" placeholder hallucinations (33% / 18%) | Both traps passed with **0% / 0% hallucinations** | Same fix as Layer 2 — once "Unavailable" is gone, the traps come back perfectly clean. |

## The four biggest wins in plain English

1. **The AI no longer pretends to know what it doesn't know.** It used to write `"Unavailable from document"` for missing fields — that counted as a made-up value. Now it just leaves the field empty. Hallucination rate dropped from 11% to 0.6%.

2. **The AI no longer writes marketing copy out of thin air.** When a proforma is too sparse and there's no supplier page to draw from, it now leaves the description blank instead of inventing a paragraph. Faithfulness rose from 73% to 93%.

3. **The fact-checker can finally read tables.** Several Ariete proforma facts used to get wrongly flagged as "unverified" because the text extractor flattened the spec table. We now hand the original PDF to the judge so it can read the table the way a human would. Ariete went from 73% to 100%.

4. **PNG proformas finally get verified properly.** Image-only invoices used to score 0% on everything because there was no text to grep against. We installed an OCR engine that reads the image into text before verification runs. The Sunon PNG went from 0% to 97% faithful; the Xiaomi PNG's hallucination rate dropped from 90% to 0%.

## What's still imperfect

| Issue | Impact | Why it didn't fix in v2 | Suggested next step |
|---|---|---|---|
| Three dense fixtures (Poco X7, Poco M7, BelAir bulk) occasionally return empty JSON from Gemini | Drags Layer 4 header consistency down ~10 points | Gemini's API itself is intermittently giving up on 5-page dense PDFs at low temperature — not something a prompt change can fix | Add a "retry once if response is empty" wrapper around the Gemini call |
| `single_belair_freezers` still only 25% faithful | One stubborn fixture | The AI still over-describes a sparse proforma even with the new rules — the source has *some* text so the strict no-source guard doesn't trigger | Add a rule: if the proforma has fewer than N printed specs, write one short paragraph instead of the full 3-4 paragraph marketing block |
| PNG headers vary slightly between runs | Pulls PNG consistency down a few points | OCR output isn't pixel-identical across runs (smart quotes, spacing) | Cache OCR text per-fixture, or add a normalisation step (collapse quotes, strip stray symbols) |

## Cost summary

| API | v1 spend | v2 spend (combined runs) |
|---|---:|---:|
| Anthropic (Haiku + Sonnet) | ~$1.60 | ~$1.20 |
| Gemini | ~$0.40 | ~$0.40 |
| Brave Search | $0 (free tier) | $0 (cached) |
| **Total** | **~$2.00** | **~$1.60** |

v2 was *cheaper* than v1 even with one extra refresh pass, because the Sonnet
judge now reads the proforma as a cached PDF document — every subsequent
claim within a fixture pays cache-read rates instead of full-input rates.
