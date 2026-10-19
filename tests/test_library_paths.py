from pathlib import Path

from src import library


def test_subs_cache_path_replaces_colons():
    p = library._subs_cache_path("movie:12345", "ar")
    assert ":" not in p.name
    assert p.name == "movie_12345.ar.srt"
    assert p.parent == library.SUBS_DIR


def test_unique_dest_numbers_collisions(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path)
    first = library._unique_dest("Test Movie (2020)", ".mkv")
    assert first == tmp_path / "movies" / "Test Movie (2020).mkv"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.touch()
    second = library._unique_dest("Test Movie (2020)", ".mkv")
    assert second.name == "Test Movie (2020) (2).mkv"


def test_safe_name_removes_path_hostile_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "LIBRARY_DIR", tmp_path)
    dest = library._unique_dest('Movie: The " Sequel <1> / x', ".mkv")
    assert "/" not in dest.name
    assert ":" not in dest.name
