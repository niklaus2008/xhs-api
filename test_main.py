import json

from main import _extract_initial_state_expr, _parse_initial_state_expr


def test_extract_window_initial_state_with_spaces() -> None:
    script = "window.__INITIAL_STATE__ = {\"a\": 1, \"b\": {\"c\": 2}};window.__FOO__=1;"
    expr = _extract_initial_state_expr(script)
    assert expr is not None
    assert json.loads(expr)["b"]["c"] == 2


def test_extract_window_bracket_initial_state() -> None:
    script = "window['__INITIAL_STATE__']={\"x\":true};"
    expr = _extract_initial_state_expr(script)
    assert expr is not None
    assert json.loads(expr)["x"] is True


def test_parse_json_parse_wrapper() -> None:
    # 这里模拟 JSON.parse(\"...\") 的形式
    expr = r'JSON.parse("{\"k\":\"v\",\"n\":1}")'
    data = _parse_initial_state_expr(expr)
    assert data["k"] == "v"
    assert data["n"] == 1


