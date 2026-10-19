import threading
from pathlib import Path

import numpy as np
import turbovec

_lock = threading.Lock()


class VectorIndex:
    def __init__(self, index: turbovec.IdMapIndex | None = None) -> None:
        self.index = index if index is not None else turbovec.IdMapIndex()
        self.count = 0

    def build(self, vectors: np.ndarray, ids: np.ndarray) -> None:
        with _lock:
            self.index = turbovec.IdMapIndex()
            self.index.add_with_ids(np.ascontiguousarray(vectors, dtype=np.float32), np.ascontiguousarray(ids, dtype=np.uint64))
            self.count = len(ids)

    def search(self, query_vec: np.ndarray, k: int, allowlist: np.ndarray | None = None) -> list[tuple[int, float]]:
        with _lock:
            queries = np.ascontiguousarray(query_vec.reshape(1, -1), dtype=np.float32)
            allowed = np.ascontiguousarray(allowlist, dtype=np.uint64) if allowlist is not None else None
            results = self.index.search(queries, k, allowlist=allowed)
        scores_out = np.asarray(results[0]).ravel()
        ids_out = np.asarray(results[1]).ravel()
        return [(int(g), float(s)) for g, s in zip(ids_out, scores_out)]

    def save(self, path: str | Path) -> None:
        self.index.write(str(path), durable=True)

    @classmethod
    def load(cls, path: str | Path) -> "VectorIndex":
        return cls(turbovec.IdMapIndex.load(str(path)))
