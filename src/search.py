import re
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


def _get_index() -> VectorIndex:
    global _index
    if _index is None:
        _index = VectorIndex.load(INDEX_PATH)
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
        if any(v.lang == lang and v.subs_lang == subs_lang for v in group.variants):
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
    "from", "by", "movie", "movies", "film", "films", "series", "show", "shows", "watch",
    "want", "watching", "something", "some", "like", "best", "good", "great", "top",
    "me", "i", "we", "you", "my", "is", "are", "was", "were", "be", "that", "this",
    "it", "its", "into", "find", "looking", "search", "suggestion", "suggestions",
}


def _query_tokens(text: str) -> list[str]:
    base = [
        t for t in re.findall(r"[a-z0-9\u0600-\u06ff]+", text.lower())
        if len(t) >= 2 and t not in STOP_WORDS
    ]
    out: list[str] = []
    for token in base:
        out.append(token)
        if len(token) > 4 and token.endswith("s"):
            out.append(token[:-1])
    return out[:10]


def keyword_search(text: str, media_type: str | None = None, k: int = 12) -> list[Group]:
    tokens = _query_tokens(text)
    if not tokens:
        return []
    phrase = " ".join(text.lower().split())
    conn = store.connect()
    rows: list[sqlite3.Row] = []
    try:
        if media_type != "series":
            conds = " OR ".join("lower(title) LIKE ?" for _ in tokens)
            rows += conn.execute(
                f"SELECT 'movie' AS kind, stream_id AS ref_id, title, year, poster, rating, lang, "
                f"ext, subs_lang, category_id, '' AS plot FROM movies WHERE {conds} "
                f"ORDER BY rating DESC LIMIT 400",
                [f"%{t}%" for t in tokens],
            ).fetchall()
        if media_type != "movie":
            conds = " OR ".join(
                "(lower(title) LIKE ? OR lower(native_title) LIKE ? OR lower(plot) LIKE ? OR lower(genre) LIKE ?)"
                for _ in tokens
            )
            params: list[str] = []
            for t in tokens:
                params += [f"%{t}%"] * 4
            rows += conn.execute(
                f"SELECT 'series' AS kind, series_id AS ref_id, title, year, poster, rating, lang, "
                f"'mkv' AS ext, NULL AS subs_lang, category_id, plot FROM series WHERE {conds} "
                f"ORDER BY rating DESC LIMIT 400",
                params,
            ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple, Group] = {}
    best: dict[tuple, float] = {}
    for row in rows:
        title = row["title"] or ""
        lowered = title.lower()
        score = 0.0
        if lowered == phrase:
            score += 120
        elif phrase and phrase in lowered:
            score += 60
        plot_l = (row["plot"] or "").lower()
        for t in tokens:
            if t in lowered:
                score += 12
            elif t in plot_l:
                score += 4
        score += float(row["rating"] or 0) * 2

        key = (row["kind"], normalize_for_grouping(title), row["year"])
        if key not in groups:
            groups[key] = Group(
                row["kind"], key[1], title, "", row["year"], row["poster"] or "",
                "" if row["kind"] == "movie" else (row["plot"] or "")[:400], score,
            )
            best[key] = score
        elif score > best[key]:
            groups[key].best_score = score
            best[key] = score

        group = groups[key]
        lang = row["lang"]
        subs = row["subs_lang"]
        if any(v.lang == lang and v.subs_lang == subs for v in group.variants):
            continue
        group.variants.append(
            Variant(lang, row["ref_id"], row["ext"] or "mkv", row["rating"] or 0, row["category_id"], subs)
        )
    return sorted(groups.values(), key=lambda g: g.best_score, reverse=True)[:k]
