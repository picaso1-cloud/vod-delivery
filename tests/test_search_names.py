from src import search


def test_stopword_titles_survive_tokenisation():
    assert search._query_tokens("from") == []
    assert search._raw_tokens("from") == ["from"]


def test_phrase_candidates_strip_qualifiers():
    cands = search._phrase_candidates("from the tv series")
    assert "from" in cands
    assert "series" not in cands
    assert "tv" not in cands
    assert cands[0] == "from the tv series"


def test_phrase_candidates_strip_trailing_year():
    assert "from" in search._phrase_candidates("from 2022")
    assert "the office" in search._phrase_candidates("the office series")


def test_phrase_candidates_keep_year_only_queries():
    assert search._phrase_candidates("2022") == ["2022"]


def test_title_core_drops_year_suffix():
    assert search._title_core("FROM (2022)") == "from"
    assert search._title_core("The Office (AU) (2024)") == "the office (au)"
    assert search._title_core("Greenland") == "greenland"


def test_stopword_is_removed_from_multiword_noise():
    cands = search._phrase_candidates("from tv show")
    assert "from" in cands
