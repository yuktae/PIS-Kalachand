"""
Product Category Classification using AI
Classifies products into 3-level hierarchical categories
"""

import json
import os
from google import genai
from google.genai import types
from .prompt_manager import get_prompt

_MODEL = 'gemini-2.0-flash'
_client = None

def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))
    return _client


def load_categories():
    """Load product categories — tries Magento API first, falls back to static JSON."""
    try:
        from .magento_api import fetch_magento_categories
        cats = fetch_magento_categories()
        if cats and len(cats) > 0:
            return cats
    except Exception as e:
        print(f"⚠ Magento API unavailable, using static categories: {e}")
    
    # Fallback to static file
    categories_file = os.path.join(os.path.dirname(__file__), 'product_categories.json')
    with open(categories_file, 'r', encoding='utf-8') as f:
        return json.load(f)


def classify_product_category(product_data):
    """
    Classify a product into 3-level categories using AI.
    
    Args:
        product_data: Dictionary containing PIS data with keys:
            - header_info: dict with product_name, brand, model_number
            - range_overview: str
            - sales_arguments: list
            - technical_specifications: dict
    
    Returns:
        Dictionary with keys: category_1, category_2, category_3
    """
    print("\n" + "="*80)
    print("🏷️ CATEGORY CLASSIFICATION STARTED")
    print("="*80)
    
    try:
        categories = load_categories()
        print(f"✓ Loaded {len(categories)} reference categories")
    except Exception as e:
        print(f"❌ ERROR loading categories: {e}")
        return get_fallback_category()
    
    # Extract relevant product information
    product_name = product_data.get('header_info', {}).get('product_name', '')
    brand = product_data.get('header_info', {}).get('brand', '')
    model_number = product_data.get('header_info', {}).get('model_number', '')
    description = product_data.get('range_overview', '')
    sales_args = product_data.get('sales_arguments', [])
    tech_specs = product_data.get('technical_specifications', {})
    
    print(f"\nProduct Info:")
    print(f"  - Name: {product_name}")
    print(f"  - Brand: {brand}")
    print(f"  - Model: {model_number}")
    print(f"  - Description: {description[:100]}..." if len(description) > 100 else f"  - Description: {description}")
    print(f"  - Sales Args Count: {len(sales_args)}")
    print(f"  - Tech Specs Count: {len(tech_specs)}")
    
    prompt = get_prompt('category_classification').format(
        product_name=product_name,
        brand=brand,
        model_number=model_number,
        description=description,
        sales_args_json=json.dumps(sales_args),
        tech_specs_json=json.dumps(tech_specs),
        categories_json=json.dumps(categories, indent=2)
    )
    
    print("\n📤 Sending request to Gemini AI...")
    
    try:
        response = _get_client().models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        
        print("📥 Received response from AI")
        print(f"Response text: {response.text[:200]}...")
        
        result = json.loads(response.text)
        
        # Extract categories
        cat1 = result.get("category_1", "").strip()
        cat2 = result.get("category_2", "").strip()
        cat3 = result.get("category_3", "").strip()
        is_custom = result.get("is_custom", False)
        
        print(f"\n🎯 AI Classification Result:")
        print(f"  - Category 1: {cat1}")
        print(f"  - Category 2: {cat2}")
        print(f"  - Category 3: {cat3}")
        print(f"  - Is Custom: {is_custom}")
        print(f"  - Reasoning: {result.get('reasoning', 'N/A')}")
        
        # Validate we have all three categories
        if not cat1 or not cat2 or not cat3:
            print("⚠️ AI returned incomplete categories, using fallback")
            return get_fallback_category()
        
        # Log the classification
        if is_custom:
            print(f"\n🆕 AI Created Custom Categories: {cat1} → {cat2} → {cat3}")
        else:
            print(f"\n✓ AI Classification (from reference): {cat1} → {cat2} → {cat3}")
        
        final_result = {
            "category_1": cat1,
            "category_2": cat2,
            "category_3": cat3
        }
        
        print(f"\n✅ Returning categories: {final_result}")
        print("="*80 + "\n")
        
        return final_result
            
    except Exception as e:
        print(f"\n❌ Category Classification Error: {e}")
        print(f"Error type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        print("="*80 + "\n")
        return get_fallback_category()


def get_fallback_category():
    """Return a safe fallback category when AI classification fails."""
    return {
        "category_1": "Home & Garden",
        "category_2": "Home Deco",
        "category_3": "Lighting"
    }


def get_unique_main_categories():
    """Get list of unique main categories (cat_A)."""
    categories = load_categories()
    return sorted(list(set(cat["cat_A"] for cat in categories)))


def get_sub_categories(main_category):
    """Get list of sub-categories (cat_B) for a given main category."""
    categories = load_categories()
    return sorted(list(set(
        cat["cat_B"] for cat in categories 
        if cat["cat_A"] == main_category
    )))


def get_sub_sub_categories(main_category, sub_category):
    """Get list of sub-sub-categories (cat_C) for given main and sub categories."""
    categories = load_categories()
    return sorted(list(set(
        cat["cat_C"] for cat in categories 
        if cat["cat_A"] == main_category and cat["cat_B"] == sub_category
    )))
