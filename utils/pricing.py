"""
Pricing config for every external AI / search API the PIS system uses.

Costs are USD per million tokens (Gemini text), per image (Gemini image),
or per 1000 queries (search APIs). Edit the table when a vendor changes
prices — utils.api_metering reads from here at call time.

Sources & dates of last verification (update these when you bump prices):
  - Gemini 2.5 Flash text:        $0.075 / 1M in, $0.30  / 1M out      (Jan 2026 list price)
  - Gemini 2.5 Flash image gen:   $0.039 per image                     (Jan 2026 list price)
  - Imagen 4.0 Standard:          $0.04  per image                     (Jan 2026 list price)
  - Google Custom Search JSON:    $5.00  / 1000 queries                (after free tier)
  - Brave Search Web API:         $3.00  / 1000 queries (Base plan)
  - DuckDuckGo:                   free
  - Internal scraper:             free
"""

from decimal import Decimal


# Per-million-token pricing (text / multimodal text-out models).
TEXT_MODEL_PRICING = {
    "gemini-2.5-flash":       {"input_per_1m": Decimal("0.075"), "output_per_1m": Decimal("0.30")},
    "gemini-2.5-flash-lite":  {"input_per_1m": Decimal("0.040"), "output_per_1m": Decimal("0.15")},
    "gemini-2.5-pro":         {"input_per_1m": Decimal("1.25"),  "output_per_1m": Decimal("5.00")},
}

# Per-image pricing (image-generation models, a.k.a. nano-banana + Imagen).
IMAGE_MODEL_PRICING = {
    "gemini-2.5-flash-image":  {"per_image": Decimal("0.039")},
    "imagen-4.0-generate-001": {"per_image": Decimal("0.040")},
}

# Per-1000-query pricing for search providers.
SEARCH_PRICING = {
    "google_cse":   {"per_1k_queries": Decimal("5.00")},
    "brave_search": {"per_1k_queries": Decimal("3.00")},
    "duckduckgo":   {"per_1k_queries": Decimal("0.00")},
    "web_scraper":  {"per_1k_queries": Decimal("0.00")},
}


def cost_for_text_call(model: str, input_tokens: int, output_tokens: int,
                       cached_tokens: int = 0) -> Decimal:
    """USD cost for a Gemini text/multimodal call. Cached tokens are
    treated as free input tokens (Gemini doesn't bill cache hits)."""
    rates = TEXT_MODEL_PRICING.get(model)
    if not rates:
        return Decimal("0")
    billable_input = max(0, (input_tokens or 0) - (cached_tokens or 0))
    in_cost  = (Decimal(billable_input) / Decimal("1000000")) * rates["input_per_1m"]
    out_cost = (Decimal(output_tokens or 0) / Decimal("1000000")) * rates["output_per_1m"]
    return (in_cost + out_cost).quantize(Decimal("0.000001"))


def cost_for_image_call(model: str, image_count: int) -> Decimal:
    """USD cost for an image-generation call (per image)."""
    rates = IMAGE_MODEL_PRICING.get(model)
    if not rates:
        return Decimal("0")
    return (Decimal(image_count or 0) * rates["per_image"]).quantize(Decimal("0.000001"))


def cost_for_search_call(provider: str, query_count: int = 1) -> Decimal:
    """USD cost for a search-API call (per query). Free providers
    return Decimal('0')."""
    rates = SEARCH_PRICING.get(provider)
    if not rates:
        return Decimal("0")
    return ((Decimal(query_count or 0) / Decimal("1000")) * rates["per_1k_queries"]) \
        .quantize(Decimal("0.000001"))


def is_image_model(model: str) -> bool:
    return model in IMAGE_MODEL_PRICING
