from src.parsing import (
    guess_title_from_filename,
    normalize_for_grouping,
    parse_movie_name,
    parse_series_name,
)


def test_movie_lang_prefix_and_year():
    r = parse_movie_name("|FR| Greenland (2020)")
    assert r["lang"] == "FR"
    assert r["title"] == "Greenland"
    assert r["year"] == 2020
    assert r["subs_lang"] is None


def test_movie_sub_prefix_is_original_audio():
    r = parse_movie_name("||SUB-AR|| Resident Evil (2002)")
    assert r["subs_lang"] == "AR"
    assert r["lang"] is None
    assert r["title"] == "Resident Evil"
    assert r["year"] == 2002


def test_movie_plain_title():
    r = parse_movie_name("Greenland (2020)")
    assert r["title"] == "Greenland"
    assert r["year"] == 2020
    assert r["lang"] is None


def test_series_lang_inside_pipes():
    r = parse_series_name("Kaamelott | 2005 | Livre I |")
    assert r["year"] == 2005
    assert r["lang"] is None
    assert r["title"] == "Kaamelott"
    assert r["native_title"] == "Livre I"


def test_series_lang_first_pipe():
    r = parse_series_name("|FR| Kaamelott | 2005 | Livre I |")
    assert r["lang"] == "FR"
    assert r["year"] == 2005
    assert r["title"] == "Kaamelott"


def test_normalize_for_grouping_folds_punct_and_case():
    assert normalize_for_grouping("Resident Evil: Apocalypse!") == "resident evil apocalypse"
    assert normalize_for_grouping("  The   Matrix ") == "the matrix"


def test_guess_title_strips_release_tags():
    title, year = guess_title_from_filename("Interstellar.2014.1080p.BluRay.x264-ETRG")
    assert title == "Interstellar"
    assert year == 2014


def test_guess_title_keeps_group_suffix_only_when_multiword():
    title, year = guess_title_from_filename("The.Matrix.1999.1080p.BluRay.x264-ETRG")
    assert title == "The Matrix"
    assert year == 1999


def test_guess_title_strips_season_episode():
    title, year = guess_title_from_filename("Il.etait.une.fois.la.Vie.S01E05.1080p")
    assert "S01E05" not in title
    assert "Vie" in title


def test_guess_title_falls_back_to_stem():
    title, year = guess_title_from_filename("weirdfile")
    assert title == "weirdfile"
    assert year is None
