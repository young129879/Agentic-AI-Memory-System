"""
Vector index backends.

The original search loads every stored vector, deserialises each one in a
Python loop, and scores them one at a time. That is O(n) work with a large
constant, and it happens on every turn: at ten thousand chunks it is already
noticeable, and a coding agent accumulates that in weeks.

Two backends replace it, chosen at runtime:

    sqlite-vec   an ANN index inside the same SQLite file, so k-NN and the
                 session filter run in one query and nothing is loaded into
                 Python that is not a result
    numpy        the same brute force as before, but vectorised into a single
                 matmul and cached, so it stays usable when sqlite-vec is not
                 installed

The interface is deliberately narrow -- add, search, delete -- so a third
backend can be dropped in without touching callers.
"""

import logging
import sqlite3
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class VectorIndex(ABC):
    """Nearest-neighbour lookup over chunk embeddings."""

    @abstractmethod
    def add(self, chunk_id: str, vector: np.ndarray, session_id: str) -> None:
        ...

    @abstractmethod
    def search(self, query: np.ndarray, top_k: int,
               min_similarity: float,
               session_id: Optional[str] = None) -> List[Tuple[str, float]]:
        """Return (chunk_id, similarity) ordered by similarity descending."""

    @abstractmethod
    def delete(self, chunk_id: str) -> None:
        ...

    def rebuild(self) -> int:
        """Repopulate from the embeddings table. Returns rows indexed."""
        return 0


def _normalise(v: np.ndarray) -> np.ndarray:
    """
    Unit-length the vector so cosine similarity becomes a dot product.

    Done once at insert rather than per comparison at query time.
    """
    v = np.asarray(v, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(v))
    return v if norm == 0.0 else v / norm


class SqliteVecIndex(VectorIndex):
    """
    ANN index stored alongside the data, via the sqlite-vec extension.

    The win is not only the index: because vectors live in SQLite, the
    session predicate and the k-NN search run in one statement. The numpy
    path has to over-fetch and filter afterwards, which either wastes work
    or silently returns too few rows.
    """

    def __init__(self, conn: sqlite3.Connection, dimension: int):
        self.conn = conn
        self.dimension = dimension
        self._load_extension(conn)
        self._init_schema()

    @staticmethod
    def _load_extension(conn: sqlite3.Connection) -> bool:
        """
        Load vec0 into this connection.

        Extensions are per-connection, not per-database, so this must run for
        every connection that will query the index -- checking availability
        elsewhere does not make vec0 visible here.
        """
        try:
            import sqlite_vec
        except ImportError:
            return False
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            return True
        except Exception as e:
            logger.debug(f"Could not load sqlite-vec: {e}")
            return False
        finally:
            try:
                conn.enable_load_extension(False)
            except Exception:
                pass

    @staticmethod
    def available(conn: sqlite3.Connection) -> bool:
        if not SqliteVecIndex._load_extension(conn):
            return False
        try:
            conn.execute("SELECT vec_version()").fetchone()
            return True
        except Exception as e:
            logger.debug(f"sqlite-vec loaded but unusable: {e}")
            return False

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
                chunk_id TEXT PRIMARY KEY,
                session_id TEXT,
                embedding float[{self.dimension}]
            )
        """)
        self.conn.commit()

    def add(self, chunk_id: str, vector: np.ndarray, session_id: str) -> None:
        vec = _normalise(vector)
        if vec.shape[0] != self.dimension:
            logger.warning(
                f"Skipping {chunk_id}: dimension {vec.shape[0]} != {self.dimension}"
            )
            return
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
        cur.execute(
            "INSERT INTO chunk_vectors (chunk_id, session_id, embedding) "
            "VALUES (?, ?, ?)",
            (chunk_id, session_id, vec.tobytes()),
        )

    def search(self, query: np.ndarray, top_k: int, min_similarity: float,
               session_id: Optional[str] = None) -> List[Tuple[str, float]]:
        vec = _normalise(query)
        cur = self.conn.cursor()
        if session_id:
            rows = cur.execute(
                "SELECT chunk_id, distance FROM chunk_vectors "
                "WHERE embedding MATCH ? AND k = ? AND session_id = ? "
                "ORDER BY distance",
                (vec.tobytes(), top_k, session_id),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT chunk_id, distance FROM chunk_vectors "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (vec.tobytes(), top_k),
            ).fetchall()

        out = []
        for chunk_id, distance in rows:
            # vec0 reports L2 distance over unit vectors, where
            # d^2 = 2 - 2*cos, so cosine similarity is 1 - d^2/2.
            similarity = 1.0 - (float(distance) ** 2) / 2.0
            if similarity >= min_similarity:
                out.append((chunk_id, similarity))
        return out

    def delete(self, chunk_id: str) -> None:
        self.conn.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))

    def rebuild(self) -> int:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chunk_vectors")
        rows = cur.execute("""
            SELECT g.chunk_id, e.embedding, g.session_id
            FROM gardened_memory g
            JOIN embeddings e ON e.turn_id = g.chunk_id
        """).fetchall()
        count = 0
        for chunk_id, blob, session_id in rows:
            try:
                vec = np.frombuffer(blob, dtype=np.float32)
                self.add(chunk_id, vec, session_id or "default_session")
                count += 1
            except Exception as e:
                logger.warning(f"Could not index {chunk_id}: {e}")
        self.conn.commit()
        logger.info(f"Rebuilt vector index: {count} chunks")
        return count


class NumpyIndex(VectorIndex):
    """
    Brute force, but vectorised and cached.

    Kept because sqlite-vec is an optional dependency and an install without
    it must still work. Two changes over the original: all vectors live in one
    contiguous matrix so scoring is a single matmul instead of a Python loop,
    and the matrix is only rebuilt when the underlying rows change.

    Session filtering happens after scoring, so it over-fetches to avoid
    returning too few rows -- the cost sqlite-vec avoids.
    """

    OVERFETCH = 5

    def __init__(self, conn: sqlite3.Connection, dimension: int):
        self.conn = conn
        self.dimension = dimension
        self._matrix: Optional[np.ndarray] = None
        self._ids: List[str] = []
        self._sessions: List[str] = []
        self._dirty = True

    def add(self, chunk_id: str, vector: np.ndarray, session_id: str) -> None:
        # Vectors are read back from SQLite on rebuild, so nothing is stored
        # here; just mark the cache stale.
        self._dirty = True

    def delete(self, chunk_id: str) -> None:
        self._dirty = True

    def _ensure_loaded(self) -> None:
        if not self._dirty and self._matrix is not None:
            return
        rows = self.conn.execute("""
            SELECT g.chunk_id, e.embedding, g.session_id
            FROM gardened_memory g
            JOIN embeddings e ON e.turn_id = g.chunk_id
        """).fetchall()

        ids, sessions, vectors = [], [], []
        for chunk_id, blob, session_id in rows:
            try:
                vec = _normalise(np.frombuffer(blob, dtype=np.float32))
                if vec.shape[0] != self.dimension:
                    continue
                ids.append(chunk_id)
                sessions.append(session_id or "default_session")
                vectors.append(vec)
            except Exception:
                continue

        self._ids = ids
        self._sessions = sessions
        self._matrix = np.vstack(vectors) if vectors else None
        self._dirty = False

    def search(self, query: np.ndarray, top_k: int, min_similarity: float,
               session_id: Optional[str] = None) -> List[Tuple[str, float]]:
        self._ensure_loaded()
        if self._matrix is None:
            return []

        # Both sides are unit length, so the dot product is cosine similarity.
        scores = self._matrix @ _normalise(query)

        fetch = top_k * self.OVERFETCH if session_id else top_k
        fetch = min(fetch, len(scores))
        # argpartition finds the top-k without sorting the whole array.
        idx = np.argpartition(-scores, fetch - 1)[:fetch] if fetch < len(scores) \
            else np.arange(len(scores))
        idx = idx[np.argsort(-scores[idx])]

        out = []
        for i in idx:
            if session_id and self._sessions[i] != session_id:
                continue
            score = float(scores[i])
            if score < min_similarity:
                break
            out.append((self._ids[i], score))
            if len(out) >= top_k:
                break
        return out

    def rebuild(self) -> int:
        self._dirty = True
        self._ensure_loaded()
        return len(self._ids)


def create_index(conn: sqlite3.Connection, dimension: int,
                 prefer: str = "auto") -> VectorIndex:
    """
    Pick a backend.

    prefer: "auto" (sqlite-vec when importable), "sqlite-vec", or "numpy".
    """
    if prefer in ("auto", "sqlite-vec") and SqliteVecIndex.available(conn):
        logger.info("Vector index: sqlite-vec")
        return SqliteVecIndex(conn, dimension)
    if prefer == "sqlite-vec":
        logger.warning("sqlite-vec requested but unavailable; using numpy")
    logger.info("Vector index: numpy (install sqlite-vec for ANN search)")
    return NumpyIndex(conn, dimension)
