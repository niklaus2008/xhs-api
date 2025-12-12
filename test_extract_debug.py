import re
import json

def extract_by_balance(text, start_marker):
    start_idx = text.find(start_marker)
    if start_idx == -1:
        return None
    
    # 找到第一个 {
    brace_start = text.find("{", start_idx)
    if brace_start == -1:
        return None
        
    count = 0
    for i in range(brace_start, len(text)):
        char = text[i]
        if char == "{":
            count += 1
        elif char == "}":
            count -= 1
            if count == 0:
                return text[brace_start:i+1]
    return None

def test_extract():
    with open("debug_failed.html", "r", encoding="utf-8") as f:
        html = f.read()
    
    json_str = extract_by_balance(html, "window.__INITIAL_STATE__=")
    if json_str:
        print(f"Extracted length: {len(json_str)}")
        try:
            data = json.loads(json_str)
            print("JSON decode success!")
            # 打印一下笔记信息验证
            note = data.get('note', {})
            print(f"Note keys: {note.keys()}")
        except Exception as e:
            print(f"JSON decode failed: {e}")
            if hasattr(e, 'pos'):
                pos = e.pos
                start = max(0, pos - 20)
                end = min(len(json_str), pos + 20)
                print(f"Error context at {pos}: {json_str[start:end]!r}")
            print(f"Snippet: {json_str[:100]} ... {json_str[-100:]}")
    else:
        print("Not found")

if __name__ == "__main__":
    test_extract()
