from json_utils import safe_json_loads
import json

def test_cases():
    print("--- Starting JSON Recovery Tests ---")
    
    # CASE 1: Valid JSON
    valid = '{"name": "test", "value": 123}'
    res = safe_json_loads(valid)
    print(f"CASE 1 (Valid): {'PASS' if res and res['name'] == 'test' else 'FAIL'}")

    # CASE 2: Markdown Wrapped
    md = '```json\n{"name": "md", "status": true}\n```'
    res = safe_json_loads(md)
    print(f"CASE 2 (Markdown): {'PASS' if res and res['name'] == 'md' else 'FAIL'}")

    # CASE 3: Truncated List
    truncated = '[{"id": 1}, {"id": 2}, {"id": 3, "fail":'
    res = safe_json_loads(truncated)
    print(f"CASE 3 (Truncated List): {'PASS' if res and len(res) == 2 and res[1]['id'] == 2 else 'FAIL'}")
    if res: print(f"  - Extracted {len(res)} items from truncated list")

    # CASE 4: Trailing Comma
    trailing = '{"items": [1, 2, 3],}'
    res = safe_json_loads(trailing)
    print(f"CASE 4 (Trailing Comma): {'PASS' if res and len(res['items']) == 3 else 'FAIL'}")

    # CASE 5: leading junk
    junk = 'Here is your data: {"key": "value"}'
    res = safe_json_loads(junk)
    print(f"CASE 5 (Leading Junk): {'PASS' if res and res['key'] == 'value' else 'FAIL'}")

if __name__ == "__main__":
    test_cases()
