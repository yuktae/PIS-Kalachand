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
        "id": "pdf_screenshot_scan",
        "name": "PDF Product Image Detection",
        "description": "Scans rendered PDF page screenshots to locate and extract the bounding box of a specific product's image. Uses text labels near images to match the correct product.",
        "category": "Image Processing",
        "prompt": """You are an expert at finding specific product images in PDF documents.

TASK: Find the image/photo for THIS SPECIFIC product: "{target_model}"

⚠️ CRITICAL — MULTIPLE PRODUCTS WARNING:
This page may contain MULTIPLE different products (e.g., a table/catalog with several items side by side).
You MUST identify the CORRECT image that belongs to "{target_model}" specifically.

HOW TO IDENTIFY THE CORRECT PRODUCT:
1. Look for TEXT LABELS near each image — model numbers, product names, descriptions
2. Match those text labels to "{target_model}"
3. The correct image is the one DIRECTLY ADJACENT to or IN THE SAME COLUMN/ROW as the matching text
4. In TABLE LAYOUTS: products are usually in columns. Find the column whose header/label matches "{target_model}", then select the image in that same column
5. Do NOT just pick the largest or most prominent image — pick the one that MATCHES the product name

WHAT A VALID PRODUCT IMAGE LOOKS LIKE:
- A photograph or rendering of the physical product
- Can be a studio shot, lifestyle image, or product in packaging

WHAT TO SKIP:
- Company logos, brand badges, certification marks
- Charts, tables (the data part), text-only sections
- QR codes, barcodes
- Images that belong to a DIFFERENT product on the same page

BOUNDING BOX FORMAT:
Return the bounding box as [ymin, xmin, ymax, xmax] on a 0-1000 scale.
The box should be TIGHT around just the product image, with minimal extra space.

Output JSON:
{{ "found": true, "box_2d": [ymin, xmin, ymax, xmax], "confidence": "high" or "medium" or "low", "matched_label": "the text near the image that helped you identify it" }}
or
{{ "found": false }}"""
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
