import json
import re

def safe_json_loads(json_str, fallback=None):
    """
    Robustly parse JSON strings from AI models.
    Supports:
    - Stripping Markdown code blocks
    - Repairing common syntax errors (missing/extra commas)
    - Extracting data from truncated JSON lists
    """
    if not json_str:
        return fallback

    # 1. Strip Markdown Code Blocks
    cleaned = json_str.strip()
    if cleaned.startswith("```"):
        # Match ```json ... ``` or ``` ... ```
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
        else:
            # If no closing backticks, just strip the opening
            cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()

    # 2. Basic Cleanup
    # Remove leading/trailing non-JSON characters (like "Here is the JSON:")
    start_idx = cleaned.find('{')
    list_start_idx = cleaned.find('[')
    
    if start_idx != -1 and (list_start_idx == -1 or start_idx < list_start_idx):
        cleaned = cleaned[start_idx:]
        end_idx = cleaned.rfind('}')
        if end_idx != -1:
            cleaned = cleaned[:end_idx+1]
    elif list_start_idx != -1:
        cleaned = cleaned[list_start_idx:]
        end_idx = cleaned.rfind(']')
        if end_idx != -1:
            cleaned = cleaned[:end_idx+1]

    # 3. Attempt Standard Parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 4. Repair Strategy: Truncated Lists
    # If it's a list that was cut off, try to extract the completed objects
    if cleaned.startswith('['):
        return _parse_truncated_list(cleaned) or fallback

    # 5. Repair Strategy: Common Syntax (Trailing commas, etc.)
    try:
        # Simple regex to remove trailing commas before closing braces/brackets
        repaired = re.sub(r',\s*([\]}])', r'\1', cleaned)
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    return fallback

def _parse_truncated_list(truncated_str):
    """
    Extracts valid JSON objects from a truncated JSON list.
    Example: [{"a":1}, {"a":2}, {"a" -> returns [{"a":1}, {"a":2}]
    """
    items = []
    # Find objects using a simple brace counter
    # Note: This doesn't handle nested braces perfectly if strings contain braces,
    # but for typical AI product outputs it's usually sufficient.
    
    depth = 0
    start = -1
    
    for i, char in enumerate(truncated_str):
        if char == '{':
            if depth == 0:
                start = i
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0 and start != -1:
                obj_str = truncated_str[start:i+1]
                try:
                    items.append(json.loads(obj_str))
                except json.JSONDecodeError:
                    pass
                start = -1
                
    return items if items else None
