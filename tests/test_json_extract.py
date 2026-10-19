from src.auditor import _extract_balanced_json


def test_balanced_json_survives_nested_braces():
    content = 'thought: the movie has } inside... {"findings": [{"note": "a {nested} thing"}]} done'
    parsed = _extract_balanced_json(content, "findings")
    assert parsed == {"findings": [{"note": "a {nested} thing"}]}


def test_balanced_json_ignores_inner_close_before_key():
    content = 'earlier {"other": 1} then {"findings": []}'
    assert _extract_balanced_json(content, "findings") == {"findings": []}


def test_balanced_json_handles_escaped_quotes():
    content = '{"findings": [{"observation": "said \\"hi\\" twice"}]}'
    parsed = _extract_balanced_json(content, "findings")
    assert parsed["findings"][0]["observation"] == 'said "hi" twice'


def test_balanced_json_returns_none_when_key_missing():
    assert _extract_balanced_json("no json at all", "findings") is None
    assert _extract_balanced_json('{"unrelated": 1}', "findings") is None
