"""Anthropic LLM-as-judge helpers for the AI accuracy eval suite.

Three call sites:
- `judge_field_value`   — Haiku  — Layer 2 paraphrase rescue
                          ("does this value appear in the sources, even paraphrased?")
- `extract_claims`      — Haiku  — Layer 3 step A
                          ("split this paragraph into atomic factual claims")
- `judge_claim`         — Sonnet — Layer 3 step B
                          ("is this claim supported by the sources?")

Design notes:

- Cross-family bias: the generator is Gemini, the judges are Claude. Same-family
  judges over-approve their own model's output, so this separation is the
  point of the eval.

- Prompt caching: the sources block (proforma_text + web_context) is identical
  across every call for a given fixture. We cache it via `cache_control` so the
  per-call cost is just (small input claim + small output) after the first call.

- Temperature 0 everywhere: judges need to be deterministic. If you change a
  judgment, it should be because you changed the prompt, not because of model
  jitter.

- JSON-only output: every prompt asks for a single JSON object. We parse with
  a robust extractor that tolerates the model wrapping output in code fences
  or adding a stray sentence.

- Cost guardrail: every call increments `_call_log` so a test run can report
  total Anthropic spend. Pricing constants are visible at the top so updates
  to the published prices are a one-line change.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Lazy-imported at first use so non-judge tests don't pay the import cost.
_client = None


# ── Model + pricing (USD per 1M tokens) ──────────────────────────────────────
# Update these if Anthropic changes their published prices.
HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-4-6"

PRICING = {
    HAIKU_MODEL:  {"input": 1.00, "output": 5.00,
                   "cache_write": 1.25, "cache_read": 0.10},
    SONNET_MODEL: {"input": 3.00, "output": 15.00,
                   "cache_write": 3.75, "cache_read": 0.30},
}


# ── Call-cost tracking ───────────────────────────────────────────────────────

@dataclass
class CallLog:
    """Accumulator for one test run's Anthropic spend."""
    calls: list[dict] = field(default_factory=list)

    def add(self, model: str, usage: dict, latency_ms: int) -> None:
        self.calls.append({
            "model": model,
            "input": usage.get("input_tokens", 0),
            "output": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "cache_write": usage.get("cache_creation_input_tokens", 0),
            "latency_ms": latency_ms,
        })

    @property
    def total_cost(self) -> float:
        total = 0.0
        for c in self.calls:
            p = PRICING.get(c["model"], {})
            total += (
                c["input"]       * p.get("input", 0)        / 1_000_000
                + c["output"]      * p.get("output", 0)       / 1_000_000
                + c["cache_read"]  * p.get("cache_read", 0)   / 1_000_000
                + c["cache_write"] * p.get("cache_write", 0)  / 1_000_000
            )
        return total

    def summary(self) -> dict:
        by_model: dict[str, dict] = {}
        for c in self.calls:
            m = by_model.setdefault(c["model"], {"calls": 0, "input": 0,
                                                  "output": 0, "cache_read": 0,
                                                  "cache_write": 0})
            m["calls"] += 1
            for k in ("input", "output", "cache_read", "cache_write"):
                m[k] += c[k]
        return {
            "total_calls": len(self.calls),
            "total_cost_usd": round(self.total_cost, 4),
            "by_model": by_model,
        }


_log = CallLog()


def get_call_log() -> CallLog:
    """Test runners read this at the end to print/save a cost summary."""
    return _log


def reset_call_log() -> None:
    """Per-test isolation if needed."""
    global _log
    _log = CallLog()


# ── Client setup ─────────────────────────────────────────────────────────────

def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — add it to .env or environment."
            )
        _client = Anthropic(api_key=api_key)
    return _client


# ── Robust JSON extraction ───────────────────────────────────────────────────
# Models sometimes wrap JSON in ```json ... ``` or add a leading sentence.
# This pulls the first {...} block and parses it.

def _parse_json(text: str) -> dict:
    if not text:
        return {}
    text = text.strip()
    # Strip code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


# ── Generic call with retry + caching ────────────────────────────────────────

_PDF_CACHE: dict[str, str] = {}


def _read_pdf_b64(pdf_path: str | None) -> str | None:
    """Return the base64-encoded contents of `pdf_path`, cached per-process so
    repeat judge calls for the same fixture don't re-read the file.

    Returns None if the path is empty, missing, or not a PDF (only PDFs are
    accepted via the Anthropic document block — image-only proformas fall
    back to the text-only judge call)."""
    if not pdf_path:
        return None
    if pdf_path in _PDF_CACHE:
        return _PDF_CACHE[pdf_path]
    p = Path(pdf_path)
    if not p.exists() or p.suffix.lower() != ".pdf":
        return None
    try:
        encoded = base64.standard_b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return None
    _PDF_CACHE[pdf_path] = encoded
    return encoded


def _call(
    *,
    model: str,
    system: str,
    cached_block: str,   # the per-fixture sources — cached (empty = skip caching)
    user_block: str,     # the per-call payload — NOT cached
    proforma_pdf_path: str | None = None,
    max_tokens: int = 400,
    retries: int = 3,
) -> dict:
    """One Claude call with optional prompt caching on `cached_block`.

    When `cached_block` is empty, the user message is sent with only `user_block`
    (Anthropic rejects cache_control on empty text blocks). When it has content,
    both blocks are sent and the cached one is marked ephemeral for reuse.

    `proforma_pdf_path` — Phase 4 Fix #4. When supplied AND the file is a real
    PDF on disk, the document is attached as a cached document content block so
    Sonnet can read the actual table layout (merged cells, multi-row spec
    tables) instead of relying on plain text extraction that strips structure.

    Returns the parsed JSON dict, or {} if the model returned junk after
    all retries. Failed responses are NOT cached by `judged_or_run` because
    {} fails the "has verdict" check downstream.
    """
    client = _get_client()
    user_content: list[Any] = []
    pdf_b64 = _read_pdf_b64(proforma_pdf_path)
    if pdf_b64:
        user_content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_b64,
            },
            "cache_control": {"type": "ephemeral"},
        })
    if cached_block:
        user_content.append({
            "type": "text",
            "text": cached_block,
            "cache_control": {"type": "ephemeral"},
        })
        user_content.append({"type": "text", "text": user_block})
    else:
        user_content.append({"type": "text", "text": user_block})
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            t0 = time.time()
            resp = client.messages.create(
                model=model,
                temperature=0,
                max_tokens=max_tokens,
                system=[{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": user_content}],
            )
            latency_ms = int((time.time() - t0) * 1000)
            _log.add(model, dict(resp.usage), latency_ms)
            # resp.content is a list of mixed block types; we only care about
            # the first text block. Use getattr so non-text blocks (thinking,
            # tool_use, etc.) don't crash — they just return "".
            text = ""
            for block in resp.content or []:
                t = getattr(block, "text", None)
                if isinstance(t, str) and t:
                    text = t
                    break
            parsed = _parse_json(text)
            if parsed:
                return parsed
            # Empty parse — retry with a sterner system nudge
            last_err = ValueError(f"unparseable JSON: {text[:200]!r}")
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))   # simple backoff
    print(f"  [judge] {model} failed after {retries} retries: {last_err}")
    return {}


# ── Layer 2: paraphrase rescue (Haiku) ───────────────────────────────────────

_HAIKU_FIELD_RESCUE_SYSTEM = """\
You verify whether a single product field value is supported by source documents.

RULES:
- Paraphrases, unit conversions, abbreviations, and obvious synonyms count as supported.
  Examples: "8 kg" ≈ "8kg" ≈ "eight kilograms"; "Bluetooth 5.0" ≈ "BT 5.0";
  "Stainless steel" ≈ "S/S".
- A claim is supported if EITHER source contains it. You do not need both.
- Inventions, plausible-sounding additions, marketing fluff with no source phrase = unsupported.
- If the value is empty or "N/A", reply unsupported.
- Be strict but not pedantic. When uncertain, mark unsupported.

Reply ONLY with valid JSON, no prose:
{
  "supported": true|false,
  "source": "proforma" | "web" | "neither",
  "evidence": "<exact phrase from source, <=150 chars>"
}"""


def judge_field_value(
    field_name: str,
    field_value: Any,
    proforma_text: str,
    web_context: str,
) -> dict:
    """Layer 2 Pass B — does the value appear in either source (paraphrased OK)?
    Returns: {supported: bool, source: str, evidence: str}"""
    cached = (
        f"=== PROFORMA TEXT ===\n{proforma_text}\n\n"
        f"=== SUPPLIER WEB PAGES ===\n{web_context}"
    )
    user = (
        f"CLAIM:\nField name: {field_name}\nField value: {field_value}"
    )
    out = _call(
        model=HAIKU_MODEL,
        system=_HAIKU_FIELD_RESCUE_SYSTEM,
        cached_block=cached,
        user_block=user,
        max_tokens=300,
    )
    return {
        "supported": bool(out.get("supported", False)),
        "source": out.get("source", "neither"),
        "evidence": out.get("evidence", ""),
    }


# ── Layer 3 Pass A: atomic-claim extractor (Haiku) ───────────────────────────

_HAIKU_CLAIM_EXTRACT_SYSTEM = """\
Decompose a product description into atomic factual claims about the product.

Rules:
- One fact per claim.
- Each claim must be self-contained — replace pronouns with the product name.
- Skip purely subjective/marketing phrases ("the best", "amazing design") UNLESS
  they contain a specific fact.
- Keep each claim short (<=25 words).

Reply ONLY with valid JSON:
{
  "claims": ["<claim 1>", "<claim 2>"]
}"""


def extract_claims(product_name: str, narrative_text: str) -> list[str]:
    """Layer 3 Pass A — split a paragraph into individual factual claims."""
    if not narrative_text or not narrative_text.strip():
        return []
    out = _call(
        model=HAIKU_MODEL,
        system=_HAIKU_CLAIM_EXTRACT_SYSTEM,
        cached_block="",  # nothing reusable here
        user_block=(
            f"Product name: {product_name}\n\n"
            f"Description:\n{narrative_text}"
        ),
        # Long marketing copy (~2500 chars) produces 15-20 atomic claims;
        # 800 tokens truncates mid-string and breaks JSON parse. 2000 covers
        # the longest seo_long + range_overview we've seen with headroom.
        max_tokens=2000,
    )
    claims = out.get("claims") or []
    return [c.strip() for c in claims if isinstance(c, str) and c.strip()]


# ── Layer 3 Pass B: claim verifier (Sonnet) ──────────────────────────────────

_SONNET_CLAIM_JUDGE_SYSTEM = """\
You judge whether a factual claim about a product is supported by the provided
source documents. You must be strict.

You will receive sources in up to THREE forms:
  1. The PROFORMA PDF itself (attached as a document) — read tables, merged
     cells, and column headers visually. This is the most reliable source.
  2. A plain-text dump of the proforma — convenient for keyword search but
     LOSES table structure, so trust the PDF over the dump when they disagree.
  3. SUPPLIER WEB PAGES — text scraped from the manufacturer's product page.

A claim is supported if it appears in ANY of these sources.

DEFINITION OF "SUPPORTED":
- The claim can be directly read or trivially paraphrased from the sources.
- Unit conversions, synonyms, and standard abbreviations are fine.
- A spec printed inside a TABLE on the PDF counts as supported even if the
  plain-text dump didn't preserve the table layout.
- The claim does NOT add new facts beyond what the sources say.

DEFINITION OF "NOT SUPPORTED":
- The claim adds a fact, number, feature, or detail not in any source.
- The claim is generic marketing language ("high quality", "innovative",
  "energy efficient") without a specific spec in the sources backing it.
- The claim is a reasonable-sounding inference that goes beyond the literal sources.
- You cannot find an exact phrase or table entry in any source matching the claim.

PROCEDURE:
1. Find the strongest supporting phrase in any source (check the PDF first).
2. Compare it to the claim word-by-word.
3. If anything in the claim is NOT covered by that phrase, mark unsupported.

Reply ONLY with valid JSON:
{
  "supported": true|false,
  "evidence": "<exact source phrase, <=200 chars, or empty if unsupported>",
  "reason": "<one short sentence>"
}"""


def judge_claim(claim: str, proforma_text: str, web_context: str,
                proforma_pdf_path: str | None = None) -> dict:
    """Layer 3 Pass B — does this atomic claim survive a strict source check?

    `proforma_pdf_path` (Phase 4 Fix #4): when the proforma is a real PDF on
    disk, pass its path so Sonnet sees the original document (with table
    structure intact) in addition to the plain-text dump. Resolves false
    "unverified" flags on spec tables the text extractor couldn't read.

    Returns: {supported: bool, evidence: str, reason: str}"""
    cached = (
        f"=== PROFORMA TEXT (plain-text dump — tables may be flattened) ===\n"
        f"{proforma_text}\n\n"
        f"=== SUPPLIER WEB PAGES ===\n{web_context}"
    )
    out = _call(
        model=SONNET_MODEL,
        system=_SONNET_CLAIM_JUDGE_SYSTEM,
        cached_block=cached,
        user_block=f"CLAIM:\n{claim}",
        proforma_pdf_path=proforma_pdf_path,
        max_tokens=400,
    )
    return {
        "supported": bool(out.get("supported", False)),
        "evidence": out.get("evidence", ""),
        "reason": out.get("reason", ""),
    }
