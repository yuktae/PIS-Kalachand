"""
Prompt Manager for PIS System
Loads, saves, and retrieves AI system prompts from the database.
Falls back to DEFAULT_PROMPTS when no DB context is available.
"""

import copy

# ==================== DEFAULT PROMPTS ====================
# Factory defaults — used as fallback and for seeding the DB on first run.

DEFAULT_PROMPTS = [
    {
        "id": "pis_extraction",
        "name": "PIS Data Extraction",
        "description": "Extracts product information (name, brand, specs, sales arguments, SEO data) from uploaded documents and/or website URLs. Used when creating a single new PIS.",
        "category": "Extraction",
        "prompt": """You are an expert Product Data Specialist and Technical Researcher.

Product to research: {model_name}

TASK:
1. EXTENSIVE RESEARCH: {source_instruction}
2. FACTUAL INTEGRITY: Identify specific technical features, performance metrics, and unique selling points.
3. **STRICT RULES**:
   - DO NOT invent, assume, or hallucinate any details.
   - If a detail is not in the documents or website context, omit it or state it's unavailable.
   - **INDEPENDENT CONTENT**: This description must be standalone. NEVER refer to other products, model variations, or colors in your text. Each overview must be unique and fully populated.
4. HERO IMAGE SELECTION:
   - Review the 'IMAGE CANDIDATES' list below.
   - **CRITICAL**: Select the single URL that represents the **HERO SHOT** (main product image).
   - AVOID diagrams, technical drawings, internal components, icons, or secondary gallery thumbnails.
   - If no clear hero shot exists in the list, fallback to searching the 'WEBSITE HTML' for a high-quality img tag.

{image_candidates_str}

{web_context}

Output strictly valid JSON:
{{
    "header_info": {{
        "product_name": "String",
        "model_number": "String",
        "brand": "String",
        "price_estimate": "String"
    }},
    "found_image_url": "String (Selected Hero Shot URL, or null)",
    "seo_data": {{
        "generated_keywords": "Comma-separated string",
        "meta_title": "Max 60 chars",
        "meta_description": "Max 160 chars",
        "seo_long_description": "2 paragraphs"
    }},
    "range_overview": "A comprehensive 2-4 paragraph technical and marketing overview. Deep-dive into technology, build quality, and use cases as found in the research data.",
    "sales_arguments": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"],
    "technical_specifications": {{ "Spec Name": "Value" }},
    "warranty_service": {{ "period": "String", "coverage": "String" }}
}}"""
    },
    {
        "id": "bulk_pis_extraction",
        "name": "Bulk PIS Extraction",
        "description": "Extracts multiple product records from a single document (e.g. invoice/catalog). Each product gets its own independent description, specs, and hero image.",
        "category": "Extraction",
        "prompt": """You are an expert Product Data Specialist and Technical Researcher.
The uploaded document(s) contain a list of products (Invoice/Catalog).
Analyze ALL uploaded documents together.

Task:
1. {filter_instruction}
2. FACTUAL ENRICHMENT: Use the Website Context to identify deep specs and detailed descriptions.
3. **STRICT ACCURACY**: Do NOT hallucinate or invent features.
4. **INDEPENDENT DESCRIPTIONS**:
   - Each product must have its own standalone, unique, and comprehensive description.
   - **CRITICAL**: NEVER refer to other products in the list (e.g., AVOID "See Model X for more info" or "Refer to the overview of the cream version").
   - Every 'range_overview' must be fully populated with its own unique text, even for simple color variations.
5. HERO IMAGE SELECTION:
   - For each product, review the 'IMAGE CANDIDATES' list below.
   - **CRITICAL**: Select the single URL that represents the **HERO SHOT** (main product image).
   - AVOID diagrams, technical drawings, internal components, icons, or secondary thumbnails.

{product_filter_instruction}

{image_candidates_str}

{web_context}

Output strictly a JSON LIST of objects:
[
    {{
        "header_info": {{ "product_name": "...", "model_number": "...", "brand": "...", "price_estimate": "..." }},
        "found_image_url": "String (Selected Hero Shot URL, or null)",
        "seo_data": {{ "generated_keywords": "...", "meta_title": "...", "meta_description": "...", "seo_long_description": "2 paragraphs" }},
        "range_overview": "A comprehensive 2-4 paragraph technical and marketing overview. Deep-dive into technology, build quality, and use cases as found in the research data.",
        "sales_arguments": ["..."],
        "technical_specifications": {{ "Spec": "Value" }},
        "warranty_service": {{ "period": "...", "coverage": "..." }}
    }}
]"""
    },
    {
        "id": "proforma_extraction",
        "name": "Proforma Import Extraction",
        "description": "Unified extraction prompt for the Proforma Import workflow. Applies clustering rules (single / similar variants / distinct products) and separates document-sourced facts from AI-enriched marketing copy.",
        "category": "Extraction",
        "prompt": """You are an expert Product Data Specialist analysing a Proforma Invoice / Catalog / Spec Sheet.

EXTRACTION MODE: {extraction_mode}
{mode_instruction}

═════════════════════════ CLUSTERING ALGORITHM ═════════════════════════
A proforma can fall into ONE of three categories. Decide carefully:

1. SINGLE PRODUCT — the document describes exactly one product.
   → Output a list with ONE product object.

2. MULTIPLE SIMILAR PRODUCTS (variants of the same base model) — products
   share the same base model and only differ by minor attributes such as
   colour, size, capacity, storage, or finish.
   → Output a SINGLE product object whose `variants` array lists every
     variation (label + model_number + price). Do NOT create separate
     product objects for variants.

3. MULTIPLE DISTINCT PRODUCTS — products belong to different model lines
   or different categories.
   → Output a separate product object for EACH distinct model.

Decision rule: if two rows share the same product_name AND brand AND only
differ by a single attribute (colour, size, capacity, storage, voltage,
finish), treat them as variants of one product. Otherwise treat them as
distinct products.

═════════════════════════ DATA SEPARATION ══════════════════════════════
For every product object you MUST split data into TWO nodes:

• `source_facts` — HARD data extracted strictly from the uploaded
  document(s). Never invent values. Leave a field empty/null if the
  document does not contain it.
    - product_name, model_number, brand, price_estimate
    - quantity (if listed on the proforma)
    - warranty_period, warranty_coverage (only if printed)
    - documented_specs: dict of specs explicitly printed in the document

• `ai_enriched_details` — data you DEDUCED, RESEARCHED, or COMPOSED
  from outside the document (web context, brand knowledge, marketing
  inference).
    - range_overview (2-4 paragraph marketing/technical overview)
    - sales_arguments (5 customer-facing selling points)
    - inferred_specs: dict of plausible specs for this product type that
      were NOT printed in the document
    - seo_data: must include generated_keywords, meta_title (≤60 chars), meta_description (≤160 chars), seo_long_description
    - found_image_url: best Hero Shot URL from the candidates below, or null
    - notes: short note describing assumptions made (optional)

{brand_context}

═════════════════════════ STRICT RULES ═════════════════════════════════
- Never put deduced data into `source_facts`. If you cannot point to an
  exact location on the page where a value was printed, it MUST go in
  `ai_enriched_details` instead.
- Never duplicate `documented_specs` keys inside `inferred_specs`.
- **Off-category inference is forbidden**: only infer specs that are
  fundamental to THIS product's category. Do not invent attributes that
  do not apply (no "Refresh Rate" for furniture, no "Cooling Capacity"
  for a TV, no "Wattage" for a wardrobe). When in doubt, leave the spec
  out — empty is always safer than misleading.
- Each product's `range_overview` must be standalone — never refer to
  other products in the list.
- For HERO IMAGE selection: pick the URL representing a clean main
  product photo. Avoid diagrams, internal components, badges, icons.
- Documents may mix French and English (Mauritius). Output every
  narrative field in English regardless of source language.

═════════════════════════ STRUCTURED DATA (PRIORITY) ═══════════════════
If a STRUCTURED DATA block is included below, those values came from the
website's own JSON-LD / OpenGraph metadata and are AUTHORITATIVE — they
override anything you read from the rendered HTML. Use them verbatim
inside `source_facts` for: product_name, brand, model_number/sku, price,
description (truncated for `range_overview`), and image URL.

{image_candidates_str}

{web_context}

═════════════════════════ CONFIDENCE SCORING ═══════════════════════════
For every narrative field you produce inside `ai_enriched_details`,
include a corresponding confidence score 0-100 in the
`confidence_scores` object:
  - 90-100: directly supported by the document or structured data
  - 60-89:  reasonable inference from product type / brand / category
  - 0-59:   weak inference, reviewer must double-check

═════════════════════════ OUTPUT FORMAT ════════════════════════════════
Output strictly a JSON object of this shape:

{{
  "products": [
    {{
      "source_facts": {{
        "product_name": "String",
        "model_number": "String",
        "brand": "String",
        "price_estimate": "String",
        "quantity": "String (or null)",
        "warranty_period": "String (or null)",
        "warranty_coverage": "String (or null)",
        "documented_specs": {{ "Spec Name": "Value" }}
      }},
      "ai_enriched_details": {{
        "range_overview": "2-4 paragraph overview",
        "sales_arguments": ["Point 1", "Point 2", "Point 3", "Point 4", "Point 5"],
        "inferred_specs": {{ "Spec Name": "Value" }},
        "seo_data": {{
          "generated_keywords": "comma-separated keywords",
          "meta_title": "≤60 chars",
          "meta_description": "≤160 chars",
          "seo_long_description": "2 paragraphs"
        }},
        "found_image_url": "String or null",
        "notes": "Optional short note about assumptions",
        "confidence_scores": {{
          "range_overview": 0,
          "sales_arguments": 0,
          "inferred_specs": 0,
          "seo_data": 0
        }}
      }},
      "variants": [
        {{ "label": "e.g. Black 256GB", "model_number": "...", "price": "..." }}
      ]
    }}
  ]
}}"""
    },
    {
        "id": "proforma_rework",
        "name": "Proforma Rework with Feedback",
        "description": "Re-runs proforma extraction taking the previous AI output and the reviewer's feedback into account. Used by the Rework button on the review modal.",
        "category": "Extraction",
        "prompt": """You previously extracted product data from a proforma document. The reviewer has provided feedback. Re-extract using the SAME rules and OUTPUT FORMAT as `proforma_extraction`, but apply the feedback below.

EXTRACTION MODE: {extraction_mode}
{mode_instruction}

═════════════════════════ PREVIOUS EXTRACTION ══════════════════════════
{prior_data_json}

═════════════════════════ REVIEWER FEEDBACK ════════════════════════════
{feedback}

═════════════════════════ INSTRUCTIONS ═════════════════════════════════
1. Re-read the uploaded document(s) carefully.
2. Apply the reviewer's feedback (e.g. "you missed the blender on page 2",
   "merge the red and blue versions", "split this into two products").
3. Preserve any product entries the reviewer did not complain about — do
   NOT regress good fields.
4. Keep the same `source_facts` vs `ai_enriched_details` separation.
5. Apply the same Clustering Algorithm (Single / Variants / Distinct).

{brand_context}

{image_candidates_str}

{web_context}

Output strictly a JSON object:
{{
  "products": [ {{ "source_facts": {{...}}, "ai_enriched_details": {{...}}, "variants": [...] }} ]
}}"""
    },
    {
        "id": "spec_sheet_generation",
        "name": "Spec Sheet Content Generation",
        "description": "Takes PIS sales arguments and rewrites them into customer-friendly, benefit-driven features. Also generates SEO metadata optimized for the Mauritius market.",
        "category": "Content Creation",
        "prompt": """You are a Senior Marketing Copywriter and SEO Specialist for J. Kalachand, Mauritius.

SOURCE DATA (PIS sales arguments – factual, internal):
{sales_arguments_json}

TASK:
Rewrite EACH sales argument into a customer-friendly, benefit-driven feature.

CRITICAL RULES:
- Maintain one-to-one mapping (same number of items in, same number out)
- Do NOT add or remove items
- Do NOT merge multiple points into one
- Keep each output item concise and persuasive
- Focus on customer benefits, not technical specs
- **FACTUAL INTEGRITY**: Use ONLY the provided source data. Do NOT invent or hallucinate any details.
{forbidden_instruction}

Also create:
1. A detailed 3-4 paragraph customer-facing product description focused on lifestyle benefits and technical excellence.
2. SEO metadata optimized for MAURITIUS market specific keywords.

SEO REQUIREMENTS:
- Keywords MUST focus on Mauritius-specific search terms
- Include local buying intent keywords like "buy in Mauritius", "Mauritius price", "delivery in Mauritius"
- Add product category + "Mauritius" combinations
- Include brand name + location combinations
- Target both English and common local search patterns

OUTPUT JSON FORMAT:
{{
    "customer_friendly_description": "A detailed 3-4 paragraph persuasive and factual description...",
    "key_features": ["Customer-friendly rewrite of argument 1", "Customer-friendly rewrite of argument 2", ...],
    "internal_web_keywords": "comma-separated list of short keywords for internal website search (e.g., 'fridge, samsung, refrigerator, silver')",
    "seo": {{
        "meta_title": "Product Name | Mauritius (60 chars max)",
        "meta_description": "Compelling description with Mauritius location (160 chars max)",
        "keywords": "product+mauritius, brand+mauritius, buy+mauritius, delivery+mauritius, mauritius price, island-wide, etc."
    }}
}}"""
    },
    {
        "id": "spec_optimization",
        "name": "Spec Sheet Optimization",
        "description": "Reviews and refines PIS data for PDF spec sheet output. Suggests additional niche keywords and verifies meta description length.",
        "category": "Content Creation",
        "prompt": """Review this PIS data: {product_data_json}.
1. Refine 'seo_long_description' for a PDF SpecSheet.
2. Suggest 5 additional niche keywords.
3. Verify 'meta_description' < 160 chars.
Output JSON: {{ "refined_description": "", "long_tail_keywords": "", "final_meta_check": "" }}"""
    },
    {
        "id": "ai_revision",
        "name": "AI Content Revision",
        "description": "Rewrites product content (description, sales arguments, specs, SEO) based on the Director's feedback comments. Ensures correct data types are maintained.",
        "category": "Content Creation",
        "prompt": """You are a professional product copywriter.

TASK:
Rewrite the following "{section_name}" content based STRICTLY on the Director's feedback.

ORIGINAL CONTENT:
{original_content}

DIRECTOR FEEDBACK:
"{director_comment}"

RULES:
- {format_instr}
- Do NOT include markdown formatting.
- Do NOT explain anything.
- Output ONLY the final result.

IMPORTANT:
- If section is "sales_arguments", output MUST be a JSON array.
- If section is "technical_specifications", output MUST be a JSON object.
- If section is "header_info", keep keys:
  product_name, model_number, brand, price_estimate
- If section is "seo_optimization", output MUST be a JSON object with keys:
  meta_title, meta_description, keywords, refined_description"""
    },
    {
        "id": "image_match_verification",
        "name": "Image Match Verification (Phase 2.2)",
        "description": "Yes/no check that a candidate image (a PDF crop or webpage screenshot crop) actually shows the requested product. Used immediately after extraction to catch mis-aligned crops before they get saved.",
        "category": "Image Processing",
        "prompt": """You are a strict visual quality gate.

Target product: "{target_label}"

Look at the attached image. Decide whether it clearly shows THE product named above (or a clearly equivalent variant such as a different colour/size of the same model). Be strict but fair:

ACCEPT if:
- The image is a clean photo / render of the product itself (or its packaging).
- The shape, type, and visible labels are consistent with the target product.

REJECT if:
- The image shows a different product type entirely.
- The image is mostly text, a diagram, a logo, a chart, a table, or a partial crop of the page.
- The image is mostly empty / mostly one solid colour.
- You cannot tell what the image is.

Respond ONLY with strict JSON:
{{ "match": true, "reason": "short explanation" }}
or
{{ "match": false, "reason": "short explanation" }}"""
    },
    {
        "id": "webpage_product_crop",
        "name": "Webpage Product Bounding Box (Phase 2.2)",
        "description": "Used in the screenshot fallback: given a full-page screenshot of a product detail page, returns the bounding box of the primary product photo so we can crop it out cleanly.",
        "category": "Image Processing",
        "prompt": """You are looking at a full-page screenshot of a product detail webpage.

Target product: "{target_label}"

TASK: Locate the PRIMARY product photo on this page — the hero shot the visitor sees first. Return its bounding box on a 0–1000 scale as [ymin, xmin, ymax, xmax].

PREFER:
- The largest, clearest photo of the product.
- A clean studio shot with white/light background.

AVOID:
- Logos, badges, banners, navigation, footers, related-product thumbnails, ads.
- Text blocks, charts, tables, reviews.

If no clear product photo is visible, return found=false.

Respond ONLY with strict JSON:
{{ "found": true, "box_2d": [ymin, xmin, ymax, xmax] }}
or
{{ "found": false }}"""
    },
    {
        "id": "image_validation",
        "name": "Image Validation",
        "description": "Validates whether a downloaded image is a relevant, high-quality product photo for the given product. Approves or rejects the image.",
        "category": "Image Processing",
        "prompt": """You are evaluating a potential product image for: "{product_name}".

Your goal is to be helpful and lenient. Approve the image if it looks like a professional product photo and is reasonably relevant to the product name.

Approve if:
- The product (or a very similar model/variation) is clearly featured.
- It looks like a high-quality product photo, even if it's from a review site or social media.
- The image is clean and would look good in a catalog.

Reject ONLY if:
- It is completely unrelated (e.g., a photo of a person, a landscape, or a totally different category of item).
- The image is extremely low quality, blurry, or contains heavy watermarks.
- It is a screenshot of a website rather than a direct image.

Respond ONLY with JSON:
{{ "approve": true }} or {{ "approve": false }}"""
    },
    {
        "id": "best_image_selection",
        "name": "Best Image Selection",
        "description": "Reviews multiple downloaded product images simultaneously and selects the single best 'Hero Shot' for the e-commerce catalog.",
        "category": "Image Processing",
        "prompt": """You are an expert Visual Quality Controller for an e-commerce catalog.
Product Name: "{product_name}"

TASK:
Review the attached images (labeled 1 to {image_count}) and select the SINGLE BEST 'Hero Shot'.
A 'Hero Shot' is a clean, professional, high-quality photograph of the main product.

CRITICAL RULES:
1. AVOID technical diagrams, line drawings, or sketches.
2. AVOID internally-focused images (e.g., a photo of a motor, a gear, or a control panel circuit).
3. AVOID images that are extremely blurry or watermarked.
4. PREFER images on a white or clean studio background.
5. If all images are poor or irrelevant, return "none".

Output strictly valid JSON:
{{ "best_index": 1 }} or {{ "best_index": "none" }}"""
    },
    {
        "id": "category_classification",
        "name": "Product Category Classification",
        "description": "Classifies a product into a 3-level hierarchy (Main Category → Sub Category → Specific Category) using AI analysis of product data against reference categories.",
        "category": "Classification",
        "prompt": """You are a product categorization expert for J. Kalachand, Mauritius.

PRODUCT INFORMATION:
- Product Name: {product_name}
- Brand: {brand}
- Model: {model_number}
- Description: {description}
- Key Features: {sales_args_json}
- Technical Specs: {tech_specs_json}

REFERENCE CATEGORIES (3-level hierarchy - use these as guidance):
{categories_json}

TASK:
Analyze the product information and classify it into 3-level categories.

RULES:
1. FIRST try to match the product to one of the reference categories above
2. If the product fits well into an existing category, use it exactly as listed
3. If NO good match exists in the reference list, CREATE new appropriate categories
4. Categories should follow this hierarchy: Main Category → Sub Category → Specific Category
5. Keep categories professional, clear, and aligned with e-commerce standards

OUTPUT FORMAT (strict JSON):
{{
    "category_1": "Main category (e.g., Electronics, Furniture, etc.)",
    "category_2": "Sub category (e.g., Kitchen, Bathroom, etc.)",
    "category_3": "Specific category (e.g., Blenders & Mixers, Wash Basin, etc.)",
    "reasoning": "Brief 1-sentence explanation",
    "is_custom": true or false (true if you created new categories, false if using reference categories)
}}"""
    },
    {
        "id": "bulk_variant_pis_extraction",
        "name": "Bulk Variant PIS Extraction",
        "description": "Used by the bulk wizard to extract ONE PIS that covers all variants in a cluster (e.g. a wardrobe sold in Walnut + Oak finishes). Unlike the single-product pis_extraction prompt, this one explicitly tells the AI to mention every variant in the description, list common specs once with variant-specific notes, and produce sales arguments that highlight the range of options.",
        "category": "Extraction",
        "prompt": """You are an expert Product Data Specialist building ONE Product Information Sheet that covers MULTIPLE variants of the same base product.

Base product: {primary_name}
Brand:        {brand}
Variants in this cluster (label · model_number · price):
{variants_block}

═════════════════════════ TASK ═════════════════════════
Read the uploaded document(s) and produce ONE PIS covering ALL of the variants above. The PIS must:

0. **header_info** —
   - `product_name`: a GENERAL family name covering every variant — DO NOT use a single variant's label. Strip variant-only suffixes like colour/finish/size from the primary name (e.g. given variants "2D Wardrobe Oak/Warm White" + "2D Wardrobe Felix Walnut", the family name is "2D Wardrobe", NOT either individual finish).
   - `model_number`: a comma-separated list of EVERY variant SKU in the cluster, in the order shown above (e.g. "XDY60.120060-OAK-W, XDY60.120060-FWAL"). Do NOT pick just one.
   - `brand`: the manufacturer brand.
   - `price_estimate`: if all variants share a price, use it; otherwise express a range like "20,385 - 32,965 MUR".

1. **range_overview** — A 2-4 paragraph technical and marketing overview that is PRECISE about each variant and explicitly calls out the slight variances between them (e.g. "available in Walnut and Oak/Warm White finishes; the 2D model measures 1200×600×2000 mm, the 3D 1500×600×2000 mm, the 4D 1800×600×2000 mm"). Don't pick one variant; describe the family AND name each member.

2. **sales_arguments** — 5 customer-facing selling points. At least one MUST highlight the choice of variants (colours, sizes, capacities, finishes — whatever differs). Where relevant, mention the differentiating attribute by name (e.g. "Choose between Walnut and Oak/Warm White finishes to match your bedroom palette").

3. **technical_specifications** — A dict where COMMON specs across all variants appear once (e.g. material, hardware, configuration). Specs that DIFFER per variant MUST be expressed as a single key whose value lists each variant's value with the variant label in parentheses, like:
   - "Dimensions": "1200×600×2000 mm (2D), 1500×600×2000 mm (3D), 1800×600×2000 mm (4D)"
   - "Color": "Walnut frame & doors (FWAL), Oak/Warm White (OAK-W)"
   - "Quantity": "26 (2D Walnut), 18 (3D Walnut), …"
   This makes variant differences scannable in the PIS without inflating the spec count.

4. **warranty_service** — `{{ "period": "...", "coverage": "..." }}`. Same for all variants unless the doc says otherwise.

5. **seo_data** — generated_keywords MUST include all variant labels for searchability. meta_title ≤60 chars, meta_description ≤160 chars, seo_long_description = 2 paragraphs covering the full range.

═════════════════════════ STRICT RULES ═════════════════════════
- DO NOT invent specs that aren't in the document or aren't fundamental to this product type.
- DO NOT pick one variant and ignore the others.
- DO NOT output a JSON list — only ONE product object covering all variants.
- Write everything in English (the source may mix French/English).

{web_context}

═════════════════════════ OUTPUT FORMAT ═════════════════════════
Output strictly valid JSON of this shape:
{{
    "header_info": {{
        "product_name":  "{primary_name}",
        "model_number":  "<base SKU or comma-separated SKUs>",
        "brand":         "{brand}",
        "price_estimate": "<range, e.g. '20,385 - 32,965 MUR'>"
    }},
    "range_overview": "2-4 paragraph overview mentioning all variants",
    "sales_arguments": ["Point 1 (must include one about variant choice)", "Point 2", "Point 3", "Point 4", "Point 5"],
    "technical_specifications": {{ "Spec Name": "Value (with variant notes if applicable)" }},
    "warranty_service": {{ "period": "String", "coverage": "String" }},
    "seo_data": {{
        "generated_keywords": "comma-separated keywords including all variant labels",
        "meta_title":         "≤60 chars",
        "meta_description":   "≤160 chars",
        "seo_long_description": "2 paragraphs"
    }}
}}"""
    },
    {
        "id": "bulk_triage_scan",
        "name": "Bulk Import Triage Scan",
        "description": "Fast classifier scan over a bulk-import proforma. Returns content density, image presence, doc origin, cluster shape, item count, and per-row preview entries (name/brand/model/price + variant group hint). Used to render the bulk preview workspace BEFORE running the full proforma extraction.",
        "category": "Extraction",
        "prompt": """You are triaging a supplier proforma for a bulk Product Information Sheet (PIS) import. This is a FAST classifier pass — not the full extraction. Be lean and conservative; the user will edit your output before the slow steps run.

KALACHAND CONTEXT (Mauritius):
J. Kalachand is a Mauritian retailer. Some proformas are external supplier docs (Hisense, Samsung, Sunon, etc.) and others are internal Kalachand-created docs with MUR pricing and no supplier branding. The user_origin_hint below tells you what the user thinks; trust it but flag mismatches.

USER ORIGIN HINT: {origin_hint}
{feedback_section}
═════════════════ TASKS ═════════════════
1. Count every product ROW in the document. One entry per row, even if rows are obvious variants of the same product.
2. For each row, extract the visibly-printed: product name, brand, model number/SKU, price (as printed, with currency).
3. Group rows that are VARIANTS of the same base product. Be AGGRESSIVE about grouping — most multi-row proformas list a base product in several finishes/sizes, and the user wants those collapsed into ONE PIS with `variants[]`, not split into many distinct PIS.

   Two rows ARE VARIANTS when ANY of these hold (one is enough):
     a. Their SKUs share a common base prefix and only differ by a suffix that encodes colour/finish/size/capacity. Example: "XDY60.120060-OAK-W" and "XDY60.120060-FWAL" share the base "XDY60.120060" → VARIANTS of the same product (the suffix names the finish).
     b. Their product names share a common stem and only differ by colour/finish/size/capacity tokens. Example: "2D WARDROBE-OAK/WARM WHITE" and "2D WARDROBE-FELIX WALNUT" share the stem "2D WARDROBE" → VARIANTS. The finish suffix DOES NOT make them distinct products.
     c. They are the same brand, the same product type (wardrobe / TV / fridge / blender / etc.), AND the only meaningful differences between them are values that fit one of these attribute axes: colour, finish, material, size, capacity, storage, voltage, RAM, screen size.

   Two rows are DISTINCT only when they describe DIFFERENT product types or DIFFERENT model lines (e.g. a 2D wardrobe is DISTINCT from a 3D wardrobe — different sizes of the wardrobe family but different model numbers and price tiers, treat as separate variant clusters; a wardrobe is DISTINCT from a TV — different categories entirely).

   Use a short, GENERAL label per variant_group that names the family WITHOUT the variant suffix (e.g. "Sunon 2D Wardrobe", NOT "Sunon 2D Wardrobe Walnut"). Distinct products get variant_group = null.

   Worked example (what the user expects):
     Rows: "2D WARDROBE-OAK-W (XDY60.120060-OAK-W)", "2D WARDROBE-FWAL (XDY60.120060-FWAL)",
           "3D WARDROBE-OAK-W (XDY63.150060-OAK-W)", "3D WARDROBE-FWAL (XDY63.150060-FWAL)",
           "4D WARDROBE-OAK-W (XDY64.180060-OAK-W)", "4D WARDROBE-FWAL (XDY64.180060-FWAL)"
     Correct output: THREE variant_groups —
       "Sunon 2D Wardrobe" (rows 1+2),
       "Sunon 3D Wardrobe" (rows 3+4),
       "Sunon 4D Wardrobe" (rows 5+6).
     Wrong output: six variant_group=null distincts. Don't do this.

4. Note whether each row has a product photo visible on the page.
5. **SOURCE PAGES**: For every row, list the ZERO-BASED page indexes where that row's product appears (text label, photo, specs, etc.). Almost always one page per row, but a row that spans page breaks may include 2 consecutive pages. Use [0] for single-page proformas. NEVER leave this field empty — at minimum return the page where the row's text/SKU appears.

═════════════════ CLASSIFY THE DOCUMENT ═════════════════
- density:
    "detailed" — rows include full descriptions, specs paragraphs, multiple printed attributes per item.
    "sparse"   — rows include only category/name/price/SKU and a short description.
    "minimal"  — rows include just name + price. No other context.
- has_images:
    "all"     — every row has a product photo on the page.
    "partial" — some rows do, some don't.
    "none"    — no product photos in the doc.
- origin:
    "external_supplier"    — clearly from an outside supplier (logo, foreign address, USD/EUR pricing common).
    "kalachand_internal"   — looks like an internal Kalachand doc (MUR pricing, Kalachand branding, no supplier letterhead).
    "unknown"              — can't tell.
- cluster_shape:
    "single"   — exactly one product detected.
    "variants" — multiple rows but they all collapse into one variant group.
    "distinct" — multiple rows, none are variants of each other.
    "mixed"    — multiple rows, some are variants and some are distinct.

═════════════════ STRICT RULES ═════════════════
- One JSON output. No prose. No markdown. JSON only.
- Never invent products that aren't in the doc.
- Never deduplicate variants — return one entry per ROW.
- If a field is missing from the doc, use empty string "" (not null) for strings, false for booleans.
- Keep names verbatim from the doc — don't translate or re-case.

═════════════════ OUTPUT FORMAT ═════════════════
{{
  "summary": {{
    "item_count": 0,
    "density":       "detailed" | "sparse" | "minimal",
    "has_images":    "all" | "partial" | "none",
    "origin":        "external_supplier" | "kalachand_internal" | "unknown",
    "cluster_shape": "single" | "variants" | "distinct" | "mixed",
    "notes":         "1 short sentence about anything noteworthy (or empty string)"
  }},
  "items": [
    {{
      "row_index":     0,
      "name":          "as printed",
      "brand":         "as printed",
      "model_number":  "as printed",
      "price":         "as printed (incl currency)",
      "category_hint": "broad category — e.g. TV, Wardrobe, Vacuum",
      "has_image":     true,
      "variant_group": "short label" or null,
      "source_pages":  [0]
    }}
  ]
}}"""
    },
    {
        "id": "pdf_screenshot_scan",
        "name": "PDF Product Image Detection",
        "description": "Scans rendered PDF page screenshots to locate and extract the bounding box of a specific product's image. Uses text labels near images to match the correct product. Returns a list so multi-view rows (e.g. open + closed wardrobe) are all captured.",
        "category": "Image Processing",
        "prompt": """You are an expert at finding specific product images in PDF documents.

TASK: Find ALL image(s)/photo(s) on this page for THIS SPECIFIC product: "{target_model}"

⚠️ CRITICAL — MULTIPLE PRODUCTS WARNING:
This page may contain MULTIPLE different products (e.g., a table/catalog with several items side by side).
You MUST identify the CORRECT image(s) that belong to "{target_model}" specifically.

HOW TO IDENTIFY THE CORRECT PRODUCT:
1. Look for TEXT LABELS near each image — model numbers, product names, descriptions
2. Match those text labels to "{target_model}"
3. The correct image is the one DIRECTLY ADJACENT to or IN THE SAME COLUMN/ROW as the matching text
4. In TABLE LAYOUTS: products are usually in columns or rows. Find the column/row whose header/label matches "{target_model}", then select EVERY image in that same column/row
5. Do NOT just pick the largest or most prominent image — pick the one(s) that MATCH the product name

⚠️ RETURN MULTIPLE PHOTOS WHEN PRESENT:
If the same product has several views on the page (e.g. open view + closed view, front + side, two angles), return a SEPARATE entry for EACH view. The user will pick one. Do NOT merge multiple photos into one bounding box.

WHAT A VALID PRODUCT IMAGE LOOKS LIKE:
- A photograph or rendering of the physical product
- Can be a studio shot, lifestyle image, or product in packaging

WHAT TO SKIP:
- Company logos, brand badges, certification marks
- Charts, tables (the data part), text-only sections
- QR codes, barcodes
- Images that belong to a DIFFERENT product on the same page

BOUNDING BOX FORMAT:
For every match, return its bounding box as [ymin, xmin, ymax, xmax] on a 0-1000 scale.
The box should be TIGHT around just the product image, with minimal extra space.

Output JSON (preferred — list of matches):
{{ "found": true, "products": [
    {{ "box_2d": [ymin, xmin, ymax, xmax], "confidence": "high|medium|low", "matched_label": "the text near the image that helped you identify it" }}
] }}

If no product image for "{target_model}" is on this page:
{{ "found": false }}"""
    },
    {
        "id": "bulk_image_routing",
        "name": "Bulk Import Image Routing",
        "description": "Routes product photos on a multi-product proforma to the correct draft PIS — one image per singleton draft, one image per variant SKU for variant clusters. Used by the bulk-import workspace's image-extraction pipeline.",
        "category": "Image Processing",
        "prompt": """You are routing product images on a supplier proforma to the correct Product Information Sheet (PIS) drafts.

DRAFTS WAITING FOR IMAGES (the "shopping list"):
{drafts_block}

The attached image is page {page_num} of the proforma. For EACH draft above, identify the image(s) on this page that depict THAT specific product. The strongest signal is the printed text label (model number, SKU, product name) directly adjacent to the photo — match by label, not by which photo looks "best".

CRITICAL RULES:
1. For VARIANT clusters (kind=variants), each variant SKU should map to ITS OWN image when distinct photos are present. Use the SKU printed next to each photo to match.
2. For SINGLETON clusters (kind=singleton), return at most ONE primary match per draft. If multiple views (open/closed, front/side) exist for the same singleton, return ONLY the clearest single view — do not duplicate.
3. If a draft has NO matching image on this page, OMIT it from the matches array. Do not invent matches. Do not return a fallback bbox.
4. Bounding boxes must be TIGHT around just the product photo — no surrounding text, no table rules, no model labels. Format: [ymin, xmin, ymax, xmax] on a 0-1000 scale.
5. `confidence` reports how certain you are about the match: "high" when the text label clearly identifies the product, "medium" when the label is partial or implied, "low" when you matched on visual similarity alone.

OUTPUT (strict JSON only - no prose, no markdown):
{{
  "matches": [
    {{
      "draft_id": 123,
      "variant_sku": "EXACT SKU printed near the photo, or empty string for singletons",
      "box_2d": [ymin, xmin, ymax, xmax],
      "matched_label": "the printed text near the image that proved the match",
      "confidence": "high" | "medium" | "low"
    }}
  ]
}}

If no images on this page belong to any of the drafts, return: {{"matches": []}}"""
    },
    {
        "id": "bulk_variant_photo_matching",
        "name": "Bulk Variant Photo Matching",
        "description": "Final-mile mapping pass for variant-cluster image extraction. Given N photos already cropped from the proforma's row band and the cluster's M variants (each with a finish/color hint parsed from its label), assigns the best-matching photo to each variant by VISIBLE finish — not by row order or text proximity. Used after the routing call to fix proformas where two variants share a merged cell but only one variant's photo is present, or where the photos are in unexpected order.",
        "category": "Image Processing",
        "prompt": """You are matching product photos to product variants. Every variant below is the same base product but differs by finish, colour, size, or material — and the proforma only printed a few photos that have to be DISTRIBUTED across the variants.

PRODUCT FAMILY: {family_label}
BRAND:          {brand}

VARIANTS (in display order):
{variants_block}

PHOTOS (attached as image_1 … image_{photo_count}, in left-to-right / top-to-bottom order as they appear on the proforma):
[see attached images]

═════════════════ RULES ═════════════════
1. Decide each variant's photo by VISIBLE FINISH. Compare the finish_hint for each variant against what you see in each photo:
   - Light wood / oak frame / white doors → matches variants whose finish_hint mentions OAK, WHITE, WARM, LIGHT, or BIRCH.
   - Dark wood / walnut / espresso / mahogany → matches variants whose finish_hint mentions WALNUT, FELIX, DARK, BROWN, ESPRESSO, or MAHOGANY.
   - Two-tone wardrobes with white doors AND a dark frame → match to whichever finish dominates the FRAME, since the doors are commonly white across the family.
2. Each variant gets EXACTLY ONE photo. If two variants both visually match the same photo, prefer the variant whose finish_hint matches MORE distinctively (e.g. "FELIX WALNUT" beats "WARM WHITE" for a walnut-frame photo). The other variant gets its second-best photo.
3. If you have FEWER photos than variants, share the closest photo with multiple variants — this is normal when the proforma only printed one finish but lists several.
4. If you have MORE photos than variants, assign each variant its single best match and ignore the extras (they'll be picked up as gallery photos).
5. Open-interior shots (showing shelves and clothes rails, no doors) match by INTERIOR wood colour, not door colour.
6. NEVER leave a variant unassigned. If you cannot tell visually, fall back to display order — variant_index N gets photo_index N when N is in range, else photo_index 1.

═════════════════ OUTPUT ═════════════════
Return strict JSON, one entry per variant:
{{
  "assignments": [
    {{"variant_index": 1, "photo_index": 2, "reason": "white doors + oak frame matches OAK/WARM WHITE finish_hint"}},
    {{"variant_index": 2, "photo_index": 1, "reason": "walnut frame matches FELIX WALNUT finish_hint"}}
  ]
}}

`variant_index` is 1-based, matching the order in the VARIANTS block above. `photo_index` is 1-based, matching image_1 … image_{photo_count}. Both must be present in every entry."""
    },
    {
        "id": "pdf_embedded_image_selection",
        "name": "PDF Embedded Image Matching",
        "description": "Selects the correct embedded image from a PDF that matches a specific product, using page text context to differentiate between multiple products in the same document.",
        "category": "Image Processing",
        "prompt": """You are an expert Visual Quality Controller.
Product to match: "{target_model}"

PAGE TEXT CONTEXT (text found near these images in the PDF):
{context_str}

TASK:
Review the attached images (labeled 1 to {candidate_count}) extracted from a PDF document.
Select the SINGLE image that best represents THIS SPECIFIC product: "{target_model}"

⚠️ IMPORTANT: The PDF contains images of MULTIPLE DIFFERENT products.
Use the page text context above to help match the correct image to "{target_model}".
Each image was found on a specific page — cross-reference the page text with the image.

PREFER:
- The image found on the same page where "{target_model}" model number appears in the text
- Clear product photos that match the product type described by the model name
- Large, high-quality images of the actual product

AVOID:
- Images that clearly show a DIFFERENT product type
- Logos, certification marks, brand badges
- Technical diagrams, charts, text blocks

Output JSON:
{{ "best_index": 1 }} or {{ "best_index": "none" }}"""
    }
]


def _seed_db_if_empty():
    """Auto-seed the Prompt table from DEFAULT_PROMPTS if it is empty."""
    try:
        from model import db, Prompt
        if Prompt.query.count() == 0:
            for p in DEFAULT_PROMPTS:
                db.session.add(Prompt(
                    name=p['id'],
                    display_name=p.get('name', p['id']),
                    description=p.get('description', ''),
                    category=p.get('category', 'General'),
                    prompt_text=p['prompt'],
                ))
            db.session.commit()
    except Exception:
        pass


def _db_row_to_dict(row):
    """Convert a Prompt DB row to the legacy dict format expected by the app."""
    return {
        'id': row.name,
        'name': row.display_name or row.name,
        'description': row.description or '',
        'category': row.category or 'General',
        'prompt': row.prompt_text,
    }


def load_all_prompts():
    """Load all prompts from the database. Falls back to defaults if DB unavailable."""
    try:
        from model import Prompt
        _seed_db_if_empty()
        rows = Prompt.query.order_by(Prompt.id).all()
        prompts = [_db_row_to_dict(r) for r in rows]

        # Merge in any defaults that don't exist in DB yet
        existing_ids = {p['id'] for p in prompts}
        for default in DEFAULT_PROMPTS:
            if default['id'] not in existing_ids:
                prompts.append(copy.deepcopy(default))
        return prompts
    except Exception:
        return copy.deepcopy(DEFAULT_PROMPTS)


def get_prompt(prompt_id):
    """Get a single prompt's text by its string ID."""
    try:
        from model import Prompt
        row = Prompt.query.filter_by(name=prompt_id).first()
        if row:
            return row.prompt_text
    except Exception:
        pass

    for d in DEFAULT_PROMPTS:
        if d['id'] == prompt_id:
            return d['prompt']
    return None


def get_default_prompt(prompt_id):
    """Get the factory default prompt text by ID."""
    for d in DEFAULT_PROMPTS:
        if d['id'] == prompt_id:
            return d['prompt']
    return None


def save_prompt(prompt_id, new_prompt_text):
    """Save updated prompt text for a specific prompt ID."""
    try:
        from model import db, Prompt
        row = Prompt.query.filter_by(name=prompt_id).first()
        if not row:
            return False
        row.prompt_text = new_prompt_text
        db.session.commit()
        return True
    except Exception as e:
        print(f"Error saving prompt '{prompt_id}': {e}")
        return False


def reset_prompt(prompt_id):
    """Reset a prompt back to its factory default."""
    default_text = get_default_prompt(prompt_id)
    if default_text is None:
        return False
    return save_prompt(prompt_id, default_text)


def reset_all_prompts():
    """Reset ALL prompts back to factory defaults."""
    try:
        from model import db, Prompt
        for d in DEFAULT_PROMPTS:
            row = Prompt.query.filter_by(name=d['id']).first()
            if row:
                row.prompt_text = d['prompt']
        db.session.commit()
        return True
    except Exception as e:
        print(f"Error resetting all prompts: {e}")
        return False


def get_prompts_by_category():
    """Get prompts grouped by category. Returns dict of {category: [prompts]}."""
    prompts = load_all_prompts()
    category_order = ['Extraction', 'Content Creation', 'Image Processing', 'Classification']
    grouped = {cat: [] for cat in category_order}

    for p in prompts:
        cat = p.get('category', 'Other')
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(p)

    return grouped
