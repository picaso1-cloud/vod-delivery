import asyncio
import time

import numpy as np

from . import embedder, store
from .config import INDEX_LANGS
from .parsing import normalize_for_grouping, parse_movie_name, parse_series_name
from .provider import Provider
from .vector_index import VectorIndex

INDEX_PATH = store.DB_PATH.parent / "index.bin"
EMBED_BATCH = 512


def movie_gid(stream_id: int) -> int:
    return stream_id * 2


def series_gid(series_id: int) -> int:
    return series_id * 2 + 1


def decode_gid(gid: int) -> tuple[str, int]:
    if gid % 2 == 0:
        return ("movie", gid // 2)
    return ("series", (gid - 1) // 2)


async def rebuild() -> None:
    provider = Provider()
    conn = store.connect()
    try:
        t0 = time.time()
        vod = await provider.vod_streams()
        shows = await provider.series()
        print(f"fetched catalogs in {time.time() - t0:.1f}s: {len(vod)} movies, {len(shows)} series")

        conn.execute("DELETE FROM movies")
        movie_rows = []
        for m in vod:
            parsed = parse_movie_name(m.get("name", ""))
            if parsed["lang"] not in INDEX_LANGS:
                continue
            movie_rows.append(
                (
                    int(m["stream_id"]),
                    parsed["title"],
                    parsed["lang"],
                    parsed.get("subs_lang"),
                    parsed["year"],
                    m.get("category_id"),
                    m.get("stream_icon", ""),
                    float(m.get("rating") or 0),
                    m.get("container_extension", "mkv"),
                    m.get("name", ""),
                )
            )
        conn.executemany(
            "INSERT INTO movies(stream_id, title, lang, subs_lang, year, category_id, poster, rating, ext, raw_name) VALUES(?,?,?,?,?,?,?,?,?,?)",
            movie_rows,
        )

        conn.execute("DELETE FROM series")
        series_rows = []
        for s in shows:
            parsed = parse_series_name(s.get("name", ""))
            if parsed["lang"] not in INDEX_LANGS:
                continue
            series_rows.append(
                (
                    int(s["series_id"]),
                    parsed["title"],
                    parsed["native_title"],
                    parsed["lang"],
                    parsed["year"],
                    s.get("category_id"),
                    s.get("cover", ""),
                    s.get("plot", ""),
                    s.get("genre", ""),
                    s.get("releaseDate", ""),
                    float(s.get("rating") or 0),
                    s.get("name", ""),
                )
            )
        conn.executemany(
            "INSERT INTO series(series_id, title, native_title, lang, year, category_id, poster, plot, genre, release_date, rating, raw_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            series_rows,
        )
        conn.execute("DELETE FROM episodes")
        conn.execute("DELETE FROM categories")
        conn.commit()
        print(f"sqlite populated in {time.time() - t0:.1f}s")

        texts = [f"{title} ({year})" if year else title for (_, title, _, _, year, *_r) in movie_rows]
        texts += [
            f"{title} {native} ({year})" if year else f"{title} {native}".strip()
            for (_, title, native, _, year, *_rest) in series_rows
        ]
        ids = [movie_gid(r[0]) for r in movie_rows] + [series_gid(r[0]) for r in series_rows]

        t1 = time.time()
        chunks = []
        for start in range(0, len(texts), EMBED_BATCH):
            chunks.append(embedder.encode(texts[start : start + EMBED_BATCH]))
            done = min(start + EMBED_BATCH, len(texts))
            print(f"\rembedding {done}/{len(texts)} ({time.time() - t1:.0f}s)", end="", flush=True)
        vectors = np.vstack(chunks)
        print(f"\nembedded {len(texts)} titles in {time.time() - t1:.0f}s, dim={vectors.shape[1]}")

        index = VectorIndex()
        index.build(vectors, np.array(ids, dtype=np.uint64))
        index.save(INDEX_PATH)
        store.set_meta(conn, "index_count", str(len(ids)))
        store.set_meta(conn, "index_dim", str(vectors.shape[1]))
        store.set_meta(conn, "index_built_at", str(int(time.time())))
        print(f"index saved: {INDEX_PATH} ({len(ids)} vectors)")
    finally:
        conn.close()
        await provider.close()


if __name__ == "__main__":
    asyncio.run(rebuild())
