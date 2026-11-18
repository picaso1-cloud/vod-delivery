import re
import threading
import unicodedata
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from . import embedder, store
from .indexer import INDEX_PATH, decode_gid, movie_gid, series_gid
from .parsing import normalize_for_grouping
from .vector_index import VectorIndex


@dataclass
class Variant:
    lang: str | None
    ref_id: int
    ext: str
    rating: float
    category_id: int | None
    subs_lang: str | None = None


@dataclass
class Group:
    kind: str
    key: str
    title: str
    native_title: str
    year: int | None
    poster: str
    plot: str
    best_score: float
    variants: list[Variant] = field(default_factory=list)


_index = None
_index_mtime = None
_index_lock = threading.Lock()


def _get_index() -> VectorIndex:
    global _index, _index_mtime
    try:
        mtime = INDEX_PATH.stat().st_mtime
    except OSError:
        mtime = None
    with _index_lock:
        if _index is None or mtime != _index_mtime:
            _index = VectorIndex.load(INDEX_PATH)
            _index_mtime = mtime
    return _index


def _row_dicts(conn, gids: list[int]) -> dict[int, sqlite3.Row]:
    out: dict[int, sqlite3.Row] = {}
    movie_ids = [g // 2 for g in gids if g % 2 == 0]
    series_ids = [(g - 1) // 2 for g in gids if g % 2 == 1]
    for chunk_start in range(0, len(movie_ids), 900):
        chunk = movie_ids[chunk_start : chunk_start + 900]
        q = f"SELECT * FROM movies WHERE stream_id IN ({','.join('?' * len(chunk))})"
        for row in conn.execute(q, chunk):
            out[movie_gid(row["stream_id"])] = row
    for chunk_start in range(0, len(series_ids), 900):
        chunk = series_ids[chunk_start : chunk_start + 900]
        q = f"SELECT * FROM series WHERE series_id IN ({','.join('?' * len(chunk))})"
        for row in conn.execute(q, chunk):
            out[series_gid(row["series_id"])] = row
    return out


def search(query: dict, k: int = 6, depth: int = 80) -> list[Group]:
    index = _get_index()
    text = query["title"]
    if query.get("year"):
        text = f"{text} ({query['year']})"
    qvec = embedder.encode([text])

    want = query.get("media_type")
    allowlist = None
    if want in ("movie", "series"):
        col, expr = ("stream_id", "stream_id*2") if want == "movie" else ("series_id", "series_id*2+1")
        conn = store.connect()
        table = "movies" if want == "movie" else "series"
        allowlist = np.fromiter(
            (r[0] for r in conn.execute(f"SELECT {expr} FROM {table}")), dtype=np.uint64
        )
        conn.close()
        if len(allowlist) == 0:
            return []

    hits = index.search(qvec, depth, allowlist=allowlist)
    if not hits:
        return []

    conn = store.connect()
    try:
        rows = _row_dicts(conn, [g for g, _ in hits])
    finally:
        conn.close()
    scores = {g: s for g, s in hits}

    want_lang = (query.get("language") or "").upper() or None
    groups: dict[tuple, Group] = {}
    for gid, _ in hits:
        row = rows.get(gid)
        if row is None:
            continue
        kind, ref_id = decode_gid(gid)
        if kind == "movie":
            title = row["title"]
            native = ""
            lang = row["lang"]
            year = row["year"]
            poster = row["poster"]
            plot = ""
            rating = row["rating"] or 0
            ext = row["ext"]
            subs_lang = row["subs_lang"]
        else:
            title = row["title"]
            native = row["native_title"] or ""
            lang = row["lang"]
            year = row["year"]
            poster = row["poster"]
            plot = (row["plot"] or "")[:400]
            rating = row["rating"] or 0
            ext = "mkv"
            subs_lang = None
        key = (kind, normalize_for_grouping(title), year)
        if key not in groups:
            groups[key] = Group(kind, key[1], title, native, year, poster, plot, scores[gid])
        group = groups[key]
        if any(v.ref_id == ref_id for v in group.variants):
            continue
        if kind == "movie" and any(v.lang == lang and v.subs_lang == subs_lang for v in group.variants):
            continue
        group.variants.append(Variant(lang, ref_id, ext, rating, row["category_id"], subs_lang))

    want_subs = (query.get("subtitles") or "").upper() or None

    def variant_sort_key(v: Variant):
        effective = v.lang or "ORIG"
        matches = bool(want_lang) and (v.lang == want_lang or (want_lang == "EN" and v.lang is None))
        sub_penalty = 0 if (not want_subs or v.subs_lang == want_subs) else 1
        return (0 if matches else 1, sub_penalty, effective)

    result = sorted(groups.values(), key=lambda g: g.best_score, reverse=True)
    for g in result:
        g.variants.sort(key=variant_sort_key)
    return result[:k]


STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with", "about",
    "from", "by", "movie", "movies", "film", "films", "series", "show", "shows", "tv",
    "episode", "episodes", "season", "seasons",
    "في", "من", "على", "عن", "الي", "الى", "مع", "هذا", "هذه", "ذلك", "التي", "الذي",
    "مسلسل", "مسلسلات", "فيلم", "افلام", "حلقة", "حلقات", "watch",
    "want", "watching", "something", "some", "like", "best", "good", "great", "top",
    "me", "i", "we", "you", "my", "is", "are", "was", "were", "be", "that", "this",
    "it", "its", "into", "find", "looking", "search", "suggestion", "suggestions",
}

SYNONYMS = {
    "undead": ["zombie"],
    "walker": ["zombie"],
    "walkers": ["zombie"],
    "zombies": ["zombie"],
    "zombie": ["undead"],
    "survival": ["post-apocalyptic", "apocalypse"],
    "apocalypse": ["post-apocalyptic", "survival"],
    "scary": ["horror"],
    "funny": ["comedy"],
    "romantic": ["romance"],
    "heist": ["robbery", "thief"],
    "robbery": ["heist"],
    "spaceship": ["space", "sci-fi"],
    "alien": ["sci-fi"],
}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    return store.normalize_arabic(text)


def query_has_signal(text: str) -> bool:
    return bool(_query_tokens(text))


def _query_tokens(text: str) -> list[str]:
    base = [
        t for t in re.findall(r"[a-z0-9\u0600-\u06ff]+", _fold(text))
        if len(t) >= 2 and t not in STOP_WORDS
    ]
    out: list[str] = []
    for token in base:
        out.append(token)
        if len(token) > 4 and token.endswith("s"):
            out.append(token[:-1])
        stripped = store.strip_arabic_article(token)
        if stripped != token:
            out.append(stripped)
        out.extend(SYNONYMS.get(token, []))
    seen: set[str] = set()
    unique: list[str] = []
    for token in out:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique[:12]


def _chunks(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _row_allowed(kind, year, rating, lang_value, genre_value, media_type,
                 year_from, year_to, rating_min, lang, genre) -> bool:
    if media_type and kind != media_type:
        return False
    if year_from and (year or 0) < year_from:
        return False
    if year_to and (year or 0) > year_to:
        return False
    if rating_min and float(rating or 0) < rating_min:
        return False
    if lang and (lang_value or "").upper() != lang.upper():
        return False
    if genre:
        if kind == "movie" or genre.lower() not in (genre_value or "").lower():
            return False
    return True


def _raw_tokens(text: str) -> list[str]:
    return [
        t for t in re.findall(r"[a-z0-9\u0600-\u06ff]+", _fold(text))
        if len(t) >= 2
    ][:4]


def _title_core(title: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", (title or "").strip().lower()).strip()


def _phrase_candidates(text: str) -> list[str]:
    phrase = " ".join(_fold(text).split())
    if not phrase:
        return []
    candidates = [phrase]
    cleaned = phrase
    while True:
        nxt = re.sub(r"\s+(?:movie|movies|film|films|series|shows?|tv)\s*$", "", cleaned).strip()
        if nxt == cleaned or not nxt:
            break
        cleaned = nxt
    for base in (phrase, cleaned):
        stripped = re.sub(r"\s*\(?\b(?:19|20)\d{2}\)?\s*$", "", base).strip()
        bare = " ".join(t for t in base.split() if t not in STOP_WORDS).strip()
        for cand in (base, stripped, bare):
            if cand and cand not in candidates:
                candidates.append(cand)
    for token in _raw_tokens(phrase):
        if token in ("movie", "movies", "film", "films", "series", "show", "shows", "tv"):
            continue
        if token not in candidates:
            candidates.append(token)
    return candidates


def keyword_search(text: str, media_type: str | None = None, k: int = 12,
                   year_from: int = 0, year_to: int = 0, rating_min: float = 0.0,
                   lang: str = "", genre: str = "") -> list[Group]:
    tokens = _query_tokens(text) or _raw_tokens(text)
    if not tokens:
        return []
    phrase = " ".join(_fold(text).split())
    conn = store.connect()
    groups: dict[tuple, Group] = {}
    best: dict[tuple, float] = {}

    def add_row(kind, ref_id, title, year, poster, rating, row_lang, ext, subs, category, plot,
                fts_rank=0.0, bonus=0.0, native=""):
        if not year:
            year_match = re.search(r"\((\d{4})\)\s*$", title or "")
            if year_match:
                year = int(year_match.group(1))
        if not _row_allowed(kind, year, rating, row_lang, plot if kind == "series" else "",
                            media_type, year_from, year_to, rating_min, lang, genre):
            return
        lowered = store.normalize_arabic((title or "").lower())
        native_l = store.normalize_arabic((native or "").lower())
        core = _title_core(lowered)
        native_core = _title_core(native_l)
        score = min(40.0, max(0.0, -float(fts_rank)) * 0.05)
        if lowered == phrase or core == phrase or (native_l and (native_l == phrase or native_core == phrase)):
            score += 120
        elif phrase and (phrase in lowered or (native_l and phrase in native_l)):
            score += 60
        score += bonus
        plot_l = store.normalize_arabic((plot or "").lower())
        in_title = [t for t in tokens if t in lowered or (native_l and t in native_l)]
        for token in tokens:
            if token in lowered or (native_l and token in native_l):
                score += 10
            elif kind == "series" and token in plot_l:
                score += 3
        if kind == "series" and len(tokens) >= 2 and not in_title and all(t in plot_l for t in tokens):
            score += 12
        score += float(rating or 0) * 2
        key = (kind, normalize_for_grouping(title or ""), year)
        if key not in groups:
            groups[key] = Group(
                kind, key[1], title or "", native or "", year, poster or "",
                "" if kind == "movie" else (plot or "")[:400], score,
            )
            best[key] = score
        elif score > best[key]:
            groups[key].best_score = score
            best[key] = score
        group = groups[key]
        if any(v.ref_id == ref_id for v in group.variants):
            return
        if kind == "movie" and any(v.lang == row_lang and v.subs_lang == subs for v in group.variants):
            return
        group.variants.append(Variant(row_lang, ref_id, ext or "mkv", rating or 0, category, subs))

    try:
        fts_rows = []
        try:
            match = " OR ".join(f'"{t}"' for t in tokens)
            fts_rows = conn.execute(
                "SELECT kind, ref_id, bm25(search_fts) AS rank FROM search_fts "
                "WHERE search_fts MATCH ? ORDER BY rank LIMIT 600",
                (match,),
            ).fetchall()
        except sqlite3.OperationalError:
            fts_rows = []

        if fts_rows:
            ranks = {(r["kind"], r["ref_id"]): r["rank"] for r in fts_rows}
            movie_ids = [r["ref_id"] for r in fts_rows if r["kind"] == "movie"]
            series_ids = [r["ref_id"] for r in fts_rows if r["kind"] == "series"]
            if movie_ids and not genre:
                for chunk in _chunks(movie_ids, 800):
                    q = (
                        "SELECT stream_id, title, year, poster, rating, lang, ext, subs_lang, category_id "
                        f"FROM movies WHERE stream_id IN ({','.join('?' * len(chunk))})"
                    )
                    for row in conn.execute(q, chunk):
                        add_row(
                            "movie", row["stream_id"], row["title"], row["year"], row["poster"],
                            row["rating"], row["lang"], row["ext"], row["subs_lang"],
                            row["category_id"], "", ranks.get(("movie", row["stream_id"]), 0.0),
                        )
            if series_ids:
                for chunk in _chunks(series_ids, 800):
                    q = (
                        "SELECT series_id, title, native_title, year, poster, rating, lang, plot, genre, category_id "
                        f"FROM series WHERE series_id IN ({','.join('?' * len(chunk))})"
                    )
                    for row in conn.execute(q, chunk):
                        add_row(
                            "series", row["series_id"], row["title"], row["year"], row["poster"],
                            row["rating"], row["lang"], "mkv", None, row["category_id"],
                            row["plot"] or "", ranks.get(("series", row["series_id"]), 0.0),
                            native=row["native_title"] or "",
                        )

        if not groups:
            like = " OR ".join("lower(title) LIKE ?" for _ in tokens)
            params = [f"%{t}%" for t in tokens]
            if not genre:
                for row in conn.execute(
                    f"SELECT stream_id, title, year, poster, rating, lang, ext, subs_lang, category_id "
                    f"FROM movies WHERE {like} ORDER BY rating DESC LIMIT 400", params
                ):
                    add_row(
                        "movie", row["stream_id"], row["title"], row["year"], row["poster"],
                        row["rating"], row["lang"], row["ext"], row["subs_lang"],
                        row["category_id"], "",
                    )
            like2 = " OR ".join(
                "(lower(title) LIKE ? OR lower(native_title) LIKE ? OR lower(plot) LIKE ? OR lower(genre) LIKE ?)"
                for _ in tokens
            )
            params2: list[str] = []
            for token in tokens:
                params2 += [f"%{token}%"] * 4
            for row in conn.execute(
                f"SELECT series_id, title, native_title, year, poster, rating, lang, plot, genre, category_id "
                f"FROM series WHERE {like2} ORDER BY rating DESC LIMIT 400", params2
            ):
                add_row(
                    "series", row["series_id"], row["title"], row["year"], row["poster"],
                    row["rating"], row["lang"], "mkv", None, row["category_id"], row["plot"] or "",
                    native=row["native_title"] or "",
                )

        if phrase:
            candidates = _phrase_candidates(phrase)
            for cand in candidates:
                year_suffix = f"{cand} (%"
                bonus = 140.0 if cand == phrase else 120.0
                for row in conn.execute(
                    "SELECT stream_id, title, year, poster, rating, lang, ext, subs_lang, category_id "
                    "FROM movies WHERE lower(title) = ? OR lower(title) LIKE ? LIMIT 60",
                    (cand, year_suffix),
                ):
                    add_row(
                        "movie", row["stream_id"], row["title"], row["year"], row["poster"],
                        row["rating"], row["lang"], row["ext"], row["subs_lang"],
                        row["category_id"], "", bonus=bonus,
                    )
                for row in conn.execute(
                    "SELECT series_id, title, native_title, year, poster, rating, lang, plot, genre, category_id "
                    "FROM series WHERE lower(title) = ? OR lower(title) LIKE ? "
                    "OR lower(native_title) = ? OR lower(native_title) LIKE ? LIMIT 60",
                    (cand, year_suffix, cand, year_suffix),
                ):
                    add_row(
                        "series", row["series_id"], row["title"], row["year"], row["poster"],
                        row["rating"], row["lang"], "mkv", None, row["category_id"],
                        row["plot"] or "", bonus=bonus, native=row["native_title"] or "",
                    )
    finally:
        conn.close()

    return sorted(groups.values(), key=lambda g: g.best_score, reverse=True)[:k]
