"""Persistance SQLite : blocs extraits et cache de traduction.

Le cache est ce qui rend le pipeline reprenable : une traduction déjà payée
n'est jamais redemandée à l'API, même après une interruption ou un changement
d'étape en aval.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Iterator

from .models import Block

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    id    TEXT PRIMARY KEY,
    page  INTEGER NOT NULL,
    ord   INTEGER NOT NULL,
    data  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS blocks_page ON blocks(page, ord);

CREATE TABLE IF NOT EXISTS tr_cache (
    key        TEXT PRIMARY KEY,
    fr         TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def cache_key(*parts: object) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- méta ----------------------------------------------------------------

    def set_meta(self, key: str, value: object) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # --- blocs ---------------------------------------------------------------

    def put_blocks(self, blocks: Iterable[Block]) -> int:
        rows = [(b.id, b.page, b.order, json.dumps(b.to_dict(), ensure_ascii=False)) for b in blocks]
        self.conn.executemany(
            "INSERT INTO blocks(id, page, ord, data) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET page=excluded.page, ord=excluded.ord, data=excluded.data",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def iter_blocks(self, pages: Iterable[int] | None = None) -> Iterator[Block]:
        if pages is None:
            cur = self.conn.execute("SELECT data FROM blocks ORDER BY page, ord")
        else:
            pages = list(pages)
            marks = ",".join("?" * len(pages))
            cur = self.conn.execute(
                f"SELECT data FROM blocks WHERE page IN ({marks}) ORDER BY page, ord", pages
            )
        for row in cur:
            yield Block.from_dict(json.loads(row["data"]))

    def blocks(self, pages: Iterable[int] | None = None) -> list[Block]:
        return list(self.iter_blocks(pages))

    def pages(self) -> list[int]:
        cur = self.conn.execute("SELECT DISTINCT page FROM blocks ORDER BY page")
        return [row["page"] for row in cur]

    def clear_blocks(self, pages: Iterable[int] | None = None) -> None:
        if pages is None:
            self.conn.execute("DELETE FROM blocks")
        else:
            pages = list(pages)
            marks = ",".join("?" * len(pages))
            self.conn.execute(f"DELETE FROM blocks WHERE page IN ({marks})", pages)
        self.conn.commit()

    # --- cache de traduction -------------------------------------------------

    def cached(self, key: str) -> str | None:
        row = self.conn.execute("SELECT fr FROM tr_cache WHERE key=?", (key,)).fetchone()
        return row["fr"] if row else None

    def cache_put(self, key: str, fr: str, model: str) -> None:
        self.conn.execute(
            "INSERT INTO tr_cache(key, fr, model) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fr=excluded.fr, model=excluded.model",
            (key, fr, model),
        )
        self.conn.commit()

    def cache_size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM tr_cache").fetchone()["n"]
