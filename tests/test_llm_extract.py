from src.llm import _balanced_objects


def test_nested_titles_json_survives_balanced_scan():
    content = 'reasoning with } and {"intent":"recommend","titles":[{"title":"a","year":2004},{"title":"b","year":null}]}'
    objs = _balanced_objects(content)
    assert objs and objs[0]["intent"] == "recommend"
    assert objs[0]["titles"][0]["year"] == 2004


def test_multiple_objects_later_ones_extracted():
    objs = _balanced_objects('{"a": 1} middle {"intent": "search"}')
    assert {"a": 1} in objs
    assert {"intent": "search"} in objs


def test_broken_json_object_is_skipped_not_raised():
    objs = _balanced_objects('{"intent": "search", broken')
    assert objs == []
