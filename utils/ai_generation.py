"""
AI content generation utilities for PIS System
Handles all Gemini AI-powered content generation
"""

import json
import os
import time
import re
from google import genai
from google.genai import types
from .category_classifier import classify_product_category
from .json_utils import safe_json_loads
from .prompt_manager import get_prompt

_MODEL = 'gemini-2.5-flash'
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


def generate_pis_data(file_paths, model_name, url_data):
    """Generate single PIS data from uploaded file(s) and/or website data.
    
    Args:
        file_paths: A single file path string, a list of file paths, or empty list/None.
        model_name: The product model name.
        url_data: Scraped website data dict.
    """
    # Normalize file_paths
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]
    
    # 1. Upload files to Gemini (if any)
    uploaded_files = []
    for fp in file_paths:
        if fp:
            uf = _get_client().files.upload(file=fp)
            while uf.state.name == "PROCESSING":
                time.sleep(1)
                uf = _get_client().files.get(name=uf.name)
            uploaded_files.append(uf)
    
    # Context Construction
    web_context = ""
    image_candidates_str = ""
    if url_data.get('text'):
        web_context = f"WEBSITE TEXT CONTENT: {url_data['text']}\n\nWEBSITE HTML (Partial): {url_data['html']}"
        candidates = url_data.get('image_candidates', [])
        image_candidates_str = "IMAGE CANDIDATES (Ranked by crawler):\n" + "\n".join([f"- {url}" for url in candidates])

    # Build source description based on what's available
    if uploaded_files and web_context:
        source_instruction = f"Analyze ALL {len(uploaded_files)} uploaded document(s) (Proforma Invoices/Spec Sheets) AND the provided Website Context. Cross-reference information across all sources."
    elif uploaded_files:
        source_instruction = f"Analyze ALL {len(uploaded_files)} uploaded document(s) (Proforma Invoices/Spec Sheets) thoroughly."
    else:
        source_instruction = "Analyze the provided Website Context thoroughly. Extract all product details from the website data."

    # Load prompt from admin-editable prompt manager
    prompt_template = get_prompt('pis_extraction')
    prompt = prompt_template.format(
        model_name=model_name,
        source_instruction=source_instruction,
        image_candidates_str=image_candidates_str,
        web_context=web_context
    )
    
    # Build content list: prompt + any uploaded files
    content_parts = [prompt] + uploaded_files
    
    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return safe_json_loads(response.text, fallback={})


def _scrub_forbidden_words(data, forbidden_words):
    """Recursively scrub forbidden words from all string values in a dict/list."""
    if isinstance(data, str):
        for word in forbidden_words:
            # Case-insensitive whole-word replacement
            pattern = re.compile(r'\b' + re.escape(word) + r'\b', re.IGNORECASE)
            data = pattern.sub('', data)
        # Clean up double spaces and leading/trailing whitespace
        data = re.sub(r'\s{2,}', ' ', data).strip()
        return data
    elif isinstance(data, list):
        return [_scrub_forbidden_words(item, forbidden_words) for item in data]
    elif isinstance(data, dict):
        return {k: _scrub_forbidden_words(v, forbidden_words) for k, v in data.items()}
    return data

def generate_comprehensive_spec_data(pis_data, forbidden_words=None):
    """Generate comprehensive spec sheet data from PIS data.
    
    Args:
        pis_data: The PIS data dict.
        forbidden_words: Optional list of words that must NOT appear in generated text.
    """
    # Extract sales arguments for strict prompt
    sales_arguments = pis_data.get('sales_arguments', [])
    
    # Build forbidden words instruction
    forbidden_instruction = ""
    if forbidden_words and len(forbidden_words) > 0:
        words_list = ", ".join([f'"{w}"' for w in forbidden_words])
        forbidden_instruction = f"""
    
    **FORBIDDEN WORDS — CRITICAL RULE**:
    The following words/phrases are STRICTLY FORBIDDEN and MUST NOT appear anywhere in your output.
    Do NOT use these words in any form (singular, plural, capitalized, etc.):
    {words_list}
    If you need to express a similar concept, use an alternative word or rephrase entirely.
    """
    
    # Load prompt from admin-editable prompt manager
    prompt_template = get_prompt('spec_sheet_generation')
    prompt = prompt_template.format(
        sales_arguments_json=json.dumps(sales_arguments),
        forbidden_instruction=forbidden_instruction
    )
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        spec_data = safe_json_loads(response.text, fallback={})
        
        # Ensure we have a dict if fallback was used
        if not isinstance(spec_data, dict): spec_data = {}
        
        # MANDATORY SAFETY NET: Enforce fallback if AI failed
        if (
            not spec_data.get("key_features")
            or not isinstance(spec_data["key_features"], list)
            or len(spec_data["key_features"]) == 0
        ):
            print("⚠️ AI returned empty/invalid key_features, falling back to PIS sales_arguments")
            spec_data["key_features"] = sales_arguments
        
        # --- POST-PROCESSING: Scrub forbidden words from all text fields ---
        if forbidden_words and len(forbidden_words) > 0:
            spec_data = _scrub_forbidden_words(spec_data, forbidden_words)
        
        # ADD CATEGORY CLASSIFICATION
        print("\n" + "="*80)
        print("🔄 Starting Category Classification Process...")
        print("="*80)
        
        try:
            categories = classify_product_category(pis_data)
            spec_data["categories"] = categories
            print(f"✅ Categories successfully added to spec_data: {categories}")
        except Exception as e:
            print(f"❌ ERROR in category classification: {e}")
            import traceback
            traceback.print_exc()
            # Add fallback categories even on error
            spec_data["categories"] = {
                "category_1": "Home & Garden",
                "category_2": "Home Deco",
                "category_3": "Lighting"
            }
            print(f"⚠️ Using fallback categories")
        
        print("="*80 + "\n")
        
        return spec_data
        
    except Exception as e:
        print(f"Spec Generation Error: {e}")
        import traceback
        traceback.print_exc()
        
        # HARD GUARANTEE: Always return valid structure with PIS data
        fallback_data = {
            "customer_friendly_description": pis_data.get('seo_data', {}).get('seo_long_description', ''),
            "key_features": sales_arguments,  # Direct 1-to-1 fallback
            "internal_web_keywords": pis_data.get('seo_data', {}).get('generated_keywords', ''),
            "seo": {
                "meta_title": pis_data.get('seo_data', {}).get('meta_title', ''),
                "meta_description": pis_data.get('seo_data', {}).get('meta_description', ''),
                "keywords": pis_data.get('seo_data', {}).get('generated_keywords', '')
            }
        }
        
        # Try to add categories even in fallback
        print("\n🏷️ Attempting category classification in fallback mode...")
        try:
            categories = classify_product_category(pis_data)
            fallback_data["categories"] = categories
            print(f"✅ Categories added successfully in fallback: {categories}")
        except Exception as cat_error:
            print(f"❌ Category classification failed in fallback: {cat_error}")
            # Ultimate fallback categories
            fallback_data["categories"] = {
                "category_1": "Home & Garden",
                "category_2": "Home Deco",
                "category_3": "Lighting"
            }
            print(f"⚠️ Using ultimate fallback categories")
        
        return fallback_data


def generate_bulk_pis_data(file_paths, url_data, product_filter=""):
    """Generate bulk PIS data for multiple products from one or more documents.
    
    Args:
        file_paths: A single file path string, a list of file paths, or empty list/None.
        url_data: Scraped website data dict.
        product_filter: Optional newline-separated string of specific product names/models to extract.
    """
    # Normalize file_paths
    if file_paths is None:
        file_paths = []
    elif isinstance(file_paths, str):
        file_paths = [file_paths]
    
    # Upload all files to Gemini
    uploaded_files = []
    for fp in file_paths:
        if fp:
            uploaded_file = _get_client().files.upload(file=fp)
            while uploaded_file.state.name == "PROCESSING":
                time.sleep(1)
                uploaded_file = _get_client().files.get(name=uploaded_file.name)
            uploaded_files.append(uploaded_file)
    
    web_context = ""
    image_candidates_str = ""
    if url_data.get('text'):
        web_context = f"WEBSITE TEXT CONTENT: {url_data['text']}\n\nWEBSITE HTML (Partial): {url_data['html']}"
        candidates = url_data.get('image_candidates', [])
        image_candidates_str = "IMAGE CANDIDATES (Ranked by crawler):\n" + "\n".join([f"- {url}" for url in candidates])

    # Build product filter instruction
    product_filter_instruction = ""
    if product_filter:
        filter_lines = [line.strip() for line in product_filter.split('\n') if line.strip()]
        if filter_lines:
            product_list_str = "\n".join([f"- {p}" for p in filter_lines])
            product_filter_instruction = f"""
    **PRODUCT FILTER — CRITICAL**:
    Extract ONLY the following specific products. Ignore all other products in the document/URL:
{product_list_str}
    Match by product name, model number, or any close variation. If a listed product is not found, skip it.
    """

    # Load prompt from admin-editable prompt manager
    filter_instruction = "Identify ONLY the specific products listed in the PRODUCT FILTER below." if product_filter_instruction else "Identify EVERY unique product model listed."
    prompt_template = get_prompt('bulk_pis_extraction')
    prompt = prompt_template.format(
        filter_instruction=filter_instruction,
        product_filter_instruction=product_filter_instruction,
        image_candidates_str=image_candidates_str,
        web_context=web_context
    )
    
    content_parts = [prompt] + uploaded_files
    
    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=content_parts,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return safe_json_loads(response.text, fallback=[])


def generate_specsheet_optimization(product_data):
    """Generate spec sheet optimization suggestions."""
    # Load prompt from admin-editable prompt manager
    prompt_template = get_prompt('spec_optimization')
    prompt = prompt_template.format(product_data_json=json.dumps(product_data))
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return safe_json_loads(response.text, fallback={})
    except:
        return {}


def generate_ai_revision(section_name, original_content, director_comment):
    """
    Uses Gemini to rewrite content based on Director's feedback.
    Ensures correct data types:
    - sales_arguments -> List[str]
    - technical_specifications -> Dict[str, str]
    - header_info -> Dict with fixed keys
    - others -> str
    """

    # ---------- FORMAT ENFORCEMENT ----------
    if section_name == "sales_arguments":
        format_instr = (
            "Output MUST be a valid JSON array of strings.\n"
            "Each sales argument MUST be its own list item.\n"
            "Do NOT combine points into sentences.\n"
            "Do NOT return a single string."
        )
    elif isinstance(original_content, list):
        format_instr = "Output a valid JSON array of strings."
    elif isinstance(original_content, dict):
        format_instr = "Output a valid JSON object with key-value pairs."
    else:
        format_instr = "Return plain rewritten text only."

    # Load prompt from admin-editable prompt manager
    original_content_str = json.dumps(original_content, ensure_ascii=False) if isinstance(original_content, (dict, list)) else original_content
    prompt_template = get_prompt('ai_revision')
    prompt = prompt_template.format(
        section_name=section_name,
        original_content=original_content_str,
        director_comment=director_comment,
        format_instr=format_instr
    )

    try:
        response = _get_client().models.generate_content(model=_MODEL, contents=prompt)
        result = response.text.strip()

        # ---------- CLEAN MARKDOWN ----------
        if result.startswith("```"):
            result = (
                result.replace("```json", "")
                      .replace("```python", "")
                      .replace("```", "")
                      .strip()
            )

        # ---------- PARSING ----------
        try:
            parsed = json.loads(result)

            # ---------- HARD TYPE ENFORCEMENT ----------
            if section_name == "sales_arguments":
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
                return [str(parsed)]

            if isinstance(original_content, dict) and isinstance(parsed, dict):
                return parsed

            if isinstance(original_content, list) and isinstance(parsed, list):
                return parsed

            return parsed

        except Exception:
            # ---------- FAILSAFE FALLBACKS ----------
            if section_name == "sales_arguments":
                # Split common AI separators safely
                return [
                    x.strip()
                    for x in re.split(r'[;\n•\-]', result)
                    if x.strip()
                ]

            if isinstance(original_content, list):
                return [x.strip() for x in result.split("\n") if x.strip()]

            return result

    except Exception as e:
        print(f"AI Revision Error [{section_name}]: {e}")
        return original_content
