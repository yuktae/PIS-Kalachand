"""
Magento API Client for J. Kalachand
Fetches product categories from the live Magento store API.
Falls back to static product_categories.json if the API is unavailable.
"""

import os
import json
import time
import requests

# ===== CONFIGURATION =====
MAGENTO_BASE_URL = os.environ.get("MAGENTO_API_URL", "https://jkalachand.com/rest/V1")
MAGENTO_BEARER_TOKEN = os.environ.get("MAGENTO_BEARER_TOKEN", "o98hv9gscjbono30kp0dxip1qeju2qmx")

# ===== IN-MEMORY CACHE =====
_category_cache = {
    'data': None,       # Flat list of {cat_A, cat_B, cat_C}
    'tree': None,       # Hierarchical tree for dropdowns
    'raw': None,        # Raw API response
    'timestamp': 0,     # Last fetch time
    'ttl': 3600         # Cache for 1 hour
}


def fetch_magento_categories(force_refresh=False):
    """
    Fetch categories from Magento API. Returns a flat list of
    {cat_A, cat_B, cat_C} dicts matching the existing format.
    
    Uses in-memory cache with 1-hour TTL. Falls back to static JSON on error.
    """
    global _category_cache
    
    # Check cache
    if not force_refresh and _category_cache['data'] and (time.time() - _category_cache['timestamp'] < _category_cache['ttl']):
        return _category_cache['data']
    
    try:
        print("🔄 Fetching categories from Magento API...")
        
        headers = {
            'Authorization': f'Bearer {MAGENTO_BEARER_TOKEN}',
            'Accept': 'application/json'
        }
        
        response = requests.get(
            f"{MAGENTO_BASE_URL}/categories",
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        
        raw_tree = response.json()
        _category_cache['raw'] = raw_tree
        
        # Parse the hierarchical tree into flat list
        flat_categories = _parse_category_tree(raw_tree)
        
        # Also build the hierarchical tree for dropdowns
        tree = _build_dropdown_tree(raw_tree)
        
        # Cache results
        _category_cache['data'] = flat_categories
        _category_cache['tree'] = tree
        _category_cache['timestamp'] = time.time()
        
        print(f"✅ Fetched {len(flat_categories)} categories from Magento")
        return flat_categories
        
    except Exception as e:
        print(f"⚠ Magento API error: {e}")
        print("📂 Falling back to static product_categories.json")
        return _load_static_fallback()


def get_category_tree(force_refresh=False):
    """
    Get the hierarchical category tree for dropdown menus.
    Returns: {cat_A_name: {cat_B_name: [cat_C_name, ...]}}
    """
    global _category_cache
    
    # Ensure data is loaded
    if not _category_cache['tree'] or force_refresh:
        fetch_magento_categories(force_refresh)
    
    if _category_cache['tree']:
        return _category_cache['tree']
    
    # Fallback: build tree from flat categories
    flat = _load_static_fallback()
    tree = {}
    for cat in flat:
        a, b, c = cat['cat_A'], cat['cat_B'], cat['cat_C']
        if a not in tree:
            tree[a] = {}
        if b not in tree[a]:
            tree[a][b] = []
        if c not in tree[a][b]:
            tree[a][b].append(c)
    return tree


def _parse_category_tree(node, level=0, parent_names=None):
    """
    Recursively parse the Magento category tree into flat {cat_A, cat_B, cat_C} entries.
    
    Magento tree structure:
    - Level 0: Root (skip)
    - Level 1: Default Category (skip)
    - Level 2: cat_A (e.g., "Home & Garden")
    - Level 3: cat_B (e.g., "Bathroom")
    - Level 4: cat_C (e.g., "Wash Basin")
    """
    if parent_names is None:
        parent_names = []
    
    categories = []
    name = node.get('name', '')
    is_active = node.get('is_active', True)
    children = node.get('children_data', [])
    node_level = node.get('level', level)
    
    # Skip inactive categories
    if not is_active:
        return categories
    
    # Build the category path
    current_path = parent_names + [name] if node_level >= 2 else parent_names
    
    # If this is a level 4+ node (cat_C), create a flat entry
    if node_level >= 4 and len(current_path) >= 3:
        categories.append({
            'cat_A': current_path[0],  # Level 2 ancestor
            'cat_B': current_path[1],  # Level 3 ancestor
            'cat_C': current_path[2],  # Level 4 (this node)
            'magento_id': node.get('id')
        })
    
    # If this is a leaf at level 3 with no children, still create an entry
    # (some cat_B categories might not have sub-categories)
    if node_level == 3 and not children and len(current_path) >= 2:
        categories.append({
            'cat_A': current_path[0],
            'cat_B': current_path[1],
            'cat_C': current_path[1],  # Use cat_B name as cat_C
            'magento_id': node.get('id')
        })
    
    # Recurse into children
    for child in children:
        categories.extend(_parse_category_tree(child, level + 1, current_path))
    
    return categories


def _build_dropdown_tree(node, level=0, parent_a=None, parent_b=None):
    """
    Build a hierarchical tree suitable for cascading dropdowns.
    Returns: {cat_A: {cat_B: [cat_C, ...]}}
    """
    tree = {}
    name = node.get('name', '')
    is_active = node.get('is_active', True)
    children = node.get('children_data', [])
    node_level = node.get('level', level)
    
    if not is_active:
        return tree
    
    for child in children:
        child_name = child.get('name', '')
        child_level = child.get('level', 0)
        child_active = child.get('is_active', True)
        child_children = child.get('children_data', [])
        
        if not child_active:
            continue
        
        if child_level == 2:
            # This is cat_A
            sub_tree = _build_dropdown_tree(child, level + 1, parent_a=child_name)
            tree[child_name] = sub_tree.get(child_name, {})
            
        elif child_level == 3 and parent_a:
            # This is cat_B
            if parent_a not in tree:
                tree[parent_a] = {}
            
            cat_c_list = []
            for grandchild in child_children:
                if grandchild.get('is_active', True):
                    cat_c_list.append(grandchild.get('name', ''))
            
            # If no children, use the cat_B name itself
            if not cat_c_list:
                cat_c_list = [child_name]
            
            tree[parent_a][child_name] = sorted(cat_c_list)
    
    return tree


def _load_static_fallback():
    """Load the static product_categories.json as fallback."""
    try:
        categories_file = os.path.join(os.path.dirname(__file__), 'product_categories.json')
        with open(categories_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Could not load static categories: {e}")
        return []


def get_category_ids_for_path(cat_a, cat_b, cat_c):
    """
    Look up the Magento category ID for a given category path.
    Useful for future Magento product API integration.
    """
    categories = fetch_magento_categories()
    for cat in categories:
        if (cat.get('cat_A') == cat_a and 
            cat.get('cat_B') == cat_b and 
            cat.get('cat_C') == cat_c):
            return cat.get('magento_id')
    return None
