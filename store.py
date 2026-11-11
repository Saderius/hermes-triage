"""SQLite catalog of skills plus the selection log.

The store is the only catalog Hermes is supposed to learn from. Nothing here
is registered with ``ctx.register_skill``, because on Hermes v0.22.7 that
would put the skill back into the system-prompt index.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Skill:
    id: int
    name: str
    description: str
    body: str
    tags: tuple[str, ...]
    source: str
    origin_path: str
    content_hash: str
    use_count: int
    last_used_at: str | None
    refs: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "body": self.body,
            "tags": list(self.tags),
            "source": self.source,
            "origin_path": self.origin_path,
            "content_hash": self.content_hash,
            "use_count": self.use_count,
            "last_used_at": self.last_used_at,
            "refs": self.refs,
        }


class SkillStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.fts_enabled = False
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT '',
                    origin_path TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    embedding BLOB,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    refs TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS selections (
                    id INTEGER PRIMARY KEY,
                    turn_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    candidates_json TEXT NOT NULL,
                    chosen TEXT,
                    accepted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_selections_created
                    ON selections(created_at);

                CREATE TABLE IF NOT EXISTS session_flags (
                    session_id TEXT PRIMARY KEY,
                    compacted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_forces (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    probability REAL NOT NULL DEFAULT 0,
                    why TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pending_forces_session
                    ON pending_forces(session_id, id);

                CREATE TABLE IF NOT EXISTS folder_skills (
                    root TEXT NOT NULL,
                    dir TEXT NOT NULL,
                    name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (root, dir)
                );

                CREATE TABLE IF NOT EXISTS session_parents (
                    session_id TEXT PRIMARY KEY,
                    parent_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chip_sessions (
                    session_id TEXT PRIMARY KEY,
                    seen_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_attach (
                    session_id TEXT PRIMARY KEY,
                    names TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    name TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sticky_skills (
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    PRIMARY KEY (session_id, name)
                );
                """
            )
            self._ensure_refs_column()
            try:  # when the chat was last compacted (kept; the flag above is cleared on the next load)
                self._conn.execute("ALTER TABLE session_flags ADD COLUMN compacted_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:  # pending piles of chats that have no stored id yet
                self._conn.execute("ALTER TABLE pending_attach ADD COLUMN fresh INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:  # which held skills were auto-attached (they may auto-detach)
                self._conn.execute("ALTER TABLE pending_attach ADD COLUMN autos TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                pass
            self._ensure_fts()
            self._conn.commit()

    def upsert(
        self,
        *,
        name: str,
        description: str,
        body: str,
        tags: Iterable[str],
        source: str,
        origin_path: str,
        content_hash: str,
        refs: str | None = None,
    ) -> str:
        """Insert or update a skill. Returns ``inserted``, ``updated``, or ``unchanged``."""
        tag_list = [str(tag) for tag in tags]
        tags_json = json.dumps(tag_list, ensure_ascii=False)
        now = _utcnow()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, content_hash, source, origin_path, refs FROM skills WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            refs_value = refs if refs is not None else ""
            if row is None:
                cur = self._conn.execute(
                    """
                    INSERT INTO skills (
                        name, description, body, tags, source, origin_path,
                        content_hash, created_at, updated_at, refs
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, description, body, tags_json, source, origin_path, content_hash, now, now, refs_value),
                )
                self._sync_fts(int(cur.lastrowid), name, description, body, refs_value, tags_json)
                self._conn.commit()
                return "inserted"
            if refs is None:
                refs_value = str(row["refs"] or "")
            if (
                row["content_hash"] == content_hash
                and row["source"] == source
                and row["origin_path"] == origin_path
                and str(row["refs"] or "") == refs_value
            ):
                return "unchanged"
            if row["content_hash"] == content_hash and str(row["refs"] or "") == refs_value:
                self._conn.execute(
                    """
                    UPDATE skills
                       SET source = ?, origin_path = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (source, origin_path, now, row["id"]),
                )
                self._conn.commit()
                return "updated"
            self._conn.execute(
                """
                UPDATE skills
                   SET description = ?, body = ?, tags = ?, source = ?,
                       origin_path = ?, content_hash = ?, refs = ?, updated_at = ?
                 WHERE id = ?
                """,
                (description, body, tags_json, source, origin_path, content_hash, refs_value, now, row["id"]),
            )
            self._sync_fts(int(row["id"]), name, description, body, refs_value, tags_json)
            self._conn.commit()
            return "updated"

    def _ensure_refs_column(self) -> None:
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(skills)")}
        if "refs" not in columns:
            self._conn.execute("ALTER TABLE skills ADD COLUMN refs TEXT NOT NULL DEFAULT ''")

    def _ensure_fts(self) -> None:
        rebuild = True
        try:
            self._conn.execute("SELECT refs FROM skills_fts LIMIT 0")
            rebuild = False
        except sqlite3.OperationalError:
            rebuild = True
        if rebuild:
            self._conn.execute("DROP TABLE IF EXISTS skills_fts")
            try:
                self._conn.execute(
                    """
                    CREATE VIRTUAL TABLE skills_fts USING fts5(
                        name, description, body, refs, tags,
                        tokenize='porter unicode61'
                    )
                    """
                )
            except sqlite3.OperationalError:
                self.fts_enabled = False
                return
            for row in self._conn.execute("SELECT id, name, description, body, refs, tags FROM skills"):
                self._sync_fts(
                    int(row["id"]),
                    str(row["name"]),
                    str(row["description"] or ""),
                    str(row["body"] or ""),
                    str(row["refs"] or ""),
                    str(row["tags"] or "[]"),
                )
        self.fts_enabled = True

    def _sync_fts(self, row_id: int, name: str, description: str, body: str, refs: str, tags_json: str) -> None:
        if not self.fts_enabled and not self._fts_table_ready():
            return
        self._conn.execute("DELETE FROM skills_fts WHERE rowid = ?", (row_id,))
        self._conn.execute(
            """
            INSERT INTO skills_fts(rowid, name, description, body, refs, tags)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (row_id, name, description, body, refs, tags_json),
        )

    def _fts_table_ready(self) -> bool:
        try:
            self._conn.execute("SELECT refs FROM skills_fts LIMIT 0")
        except sqlite3.OperationalError:
            return False
        return True

    def get(self, name: str) -> Skill | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM skills WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
        return _skill_from_row(row) if row else None

    def catalog_signature(self) -> tuple:
        """Changes whenever a skill is added, removed or edited."""
        with self._lock:
            row = self._conn.execute(
                "SELECT count(*), coalesce(max(id), 0), coalesce(max(updated_at), '') FROM skills"
            ).fetchone()
        return tuple(row)

    def list_skills(self) -> list[Skill]:
        """Every skill. Cached per database file and reused until the catalog changes
        (any process): ranking calls this for every chunk, and loading 2,500 bodies
        each time costs far more than the ranking itself."""
        with self._lock:
            signature = tuple(
                self._conn.execute(
                    "SELECT count(*), coalesce(max(id), 0), coalesce(max(updated_at), '') FROM skills"
                ).fetchone()
            )
            key = str(self.db_path)
            cached = _SKILL_CACHE.get(key)
            if cached is not None and cached[0] == signature:
                return list(cached[1])
            rows = self._conn.execute("SELECT * FROM skills ORDER BY name COLLATE NOCASE").fetchall()
        skills = [_skill_from_row(row) for row in rows]
        _SKILL_CACHE[key] = (signature, skills)
        return list(skills)

    def delete(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM skills WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM skills WHERE id = ?", (row["id"],))
            if self.fts_enabled:
                self._conn.execute("DELETE FROM skills_fts WHERE rowid = ?", (row["id"],))
            self._conn.commit()
        return True

    def enqueue_force(self, session_id: str, name: str, probability: float = 0.0, why: str = "") -> None:
        """Remember a desktop or slash pick until the next model turn."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO pending_forces (session_id, name, probability, why, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id or "", name, float(probability or 0), why or "", now),
            )
            self._conn.commit()

    def pop_pending_force(self, session_id: str) -> dict[str, Any] | None:
        """Take the newest force for this session and drop any older ones."""
        key = session_id or ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT name, probability, why FROM pending_forces
                 WHERE session_id = ?
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (key,),
            ).fetchone()
            self._conn.execute("DELETE FROM pending_forces WHERE session_id = ?", (key,))
            self._conn.commit()
        if row is None:
            return None
        return {"name": str(row["name"]), "probability": float(row["probability"] or 0), "why": str(row["why"] or "")}

    # --- chats driven by the desktop chips ---

    def mark_chip_session(self, session_id: str) -> None:
        """The desktop chips are showing for this chat (called from /rank and /attach)."""
        import time

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO chip_sessions (session_id, seen_at) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET seen_at = excluded.seen_at
                """,
                (session_id or "", time.time()),
            )
            self._conn.commit()

    def chip_session_active(self, session_id: str, *, first_turn: bool = False, max_age: float = 600.0) -> bool:
        """True when the chips drove this chat recently. A brand-new chat has no
        stored id yet, so on its first turn any chip activity in the last two
        minutes counts."""
        import time

        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT seen_at FROM chip_sessions WHERE session_id = ?", (session_id or "",)
            ).fetchone()
            if row is not None and now - float(row["seen_at"]) <= max_age:
                return True
            if first_turn:
                row = self._conn.execute("SELECT max(seen_at) AS seen FROM chip_sessions").fetchone()
                return bool(row and row["seen"] and now - float(row["seen"]) <= 120.0)
        return False

    # --- favourites (the star in the "More" list) and most used ---

    def set_favorite(self, name: str, on: bool) -> None:
        import time

        with self._lock:
            if on:
                self._conn.execute(
                    "INSERT OR IGNORE INTO favorites (name, created_at) VALUES (?, ?)", (name, time.time())
                )
            else:
                self._conn.execute("DELETE FROM favorites WHERE name = ?", (name,))
            self._conn.commit()

    def favorites(self) -> list[str]:
        """Starred skills that still exist in the catalog, oldest star first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT skills.name FROM favorites JOIN skills ON skills.name = favorites.name
                 ORDER BY favorites.created_at ASC
                """
            ).fetchall()
        return [str(row[0]) for row in rows]

    def most_used(self, limit: int = 12) -> list[str]:
        """Skills loaded at least once, most loads first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT name FROM skills WHERE use_count > 0
                 ORDER BY use_count DESC, last_used_at DESC, name ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [str(row[0]) for row in rows]

    # --- skills a chat wants back after a compaction ---

    def set_sticky(self, session_id: str, name: str, on: bool) -> None:
        with self._lock:
            if on:
                self._conn.execute(
                    "INSERT OR IGNORE INTO sticky_skills (session_id, name) VALUES (?, ?)", (session_id or "", name)
                )
            else:
                self._conn.execute(
                    "DELETE FROM sticky_skills WHERE session_id = ? AND name = ?", (session_id or "", name)
                )
            self._conn.commit()

    def sticky(self, session_ids: list[str]) -> list[str]:
        """Names kept after compaction in any of these session ids (one chat can have several)."""
        ids = [str(sid) for sid in session_ids if sid]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT DISTINCT name FROM sticky_skills WHERE session_id IN ({marks}) ORDER BY name", tuple(ids)
            ).fetchall()
        return [str(row[0]) for row in rows]

    # --- skill folders the watcher keeps in sync (watcher.py) ---

    def folder_roots(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT root FROM folder_skills").fetchall()
        return [str(row["root"]) for row in rows]

    def folder_manifest(self, root: str) -> dict[str, tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dir, name, fingerprint FROM folder_skills WHERE root = ?", (root,)
            ).fetchall()
        return {str(row["dir"]): (str(row["name"]), str(row["fingerprint"])) for row in rows}

    def record_folder_skill(self, root: str, folder: str, name: str, fingerprint: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO folder_skills (root, dir, name, fingerprint) VALUES (?, ?, ?, ?)
                ON CONFLICT(root, dir) DO UPDATE SET name = excluded.name, fingerprint = excluded.fingerprint
                """,
                (root, folder, name, fingerprint),
            )
            self._conn.commit()

    def folder_skill_places(self, name: str) -> list[tuple[str, str]]:
        """(root, dir) of every connected folder that provides ``name``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT root, dir FROM folder_skills WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchall()
        return [(str(row["root"]), str(row["dir"])) for row in rows]

    def point_sources_at_folders(self, root: str, earlier: list[str] | tuple = ()) -> int:
        """Skills indexed from ``root`` read their files from the original folder.

        Moves catalogs made by older versions (which copied every skill into a
        mirror) onto the original folders. Returns how many rows changed."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id, f.dir, s.name FROM skills s
                  JOIN folder_skills f ON f.name = s.name COLLATE NOCASE
                 WHERE f.root = ? AND s.source != f.dir
                """,
                (root,),
            ).fetchall()
            if earlier:
                # A name an earlier-listed folder provides belongs to that folder.
                marks = ",".join("?" for _ in earlier)
                owned = {
                    str(r[0]).casefold()
                    for r in self._conn.execute(
                        f"SELECT name FROM folder_skills WHERE root IN ({marks})", tuple(earlier)
                    ).fetchall()
                }
                rows = [row for row in rows if str(row["name"]).casefold() not in owned]
            for row in rows:
                self._conn.execute("UPDATE skills SET source = ? WHERE id = ?", (row["dir"], row["id"]))
            if rows:
                self._conn.commit()
        return len(rows)

    def forget_folder_skill(self, root: str, folder: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM folder_skills WHERE root = ? AND dir = ?", (root, folder))
            self._conn.commit()

    def folder_skill_owners(self, name: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dir FROM folder_skills WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchall()
        return [str(row["dir"]) for row in rows]

    def set_pending_attach(
        self, session_id: str, names: list[str], *, fresh: bool = False, autos: list[str] | None = None
    ) -> None:
        """The skills held on the desktop's left pile for this chat's next message.

        Written by the desktop REST process, read by the agent process (they share
        this database). An empty list clears the entry."""
        import json
        import time

        key = session_id or ""
        autos_val = json.dumps([name for name in names if name.casefold() in {str(a).casefold() for a in (autos or [])}])
        with self._lock:
            if names:
                self._conn.execute(
                    """
                    INSERT INTO pending_attach (session_id, names, updated_at, fresh, autos) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET names = excluded.names,
                        updated_at = excluded.updated_at, fresh = excluded.fresh, autos = excluded.autos
                    """,
                    (key, json.dumps(list(names)), time.time(), 1 if fresh else 0, autos_val),
                )
            else:
                self._conn.execute("DELETE FROM pending_attach WHERE session_id = ?", (key,))
            self._conn.commit()

    def get_pending_attach(self, session_id: str) -> list[str]:
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT names FROM pending_attach WHERE session_id = ?", (session_id or "",)
            ).fetchone()
        if row is None:
            return []
        try:
            return [str(name) for name in json.loads(row["names"]) if name]
        except (TypeError, ValueError):
            return []

    def get_pending_autos(self, session_id: str) -> list[str]:
        """The held skills of this chat that were auto-attached."""
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT autos FROM pending_attach WHERE session_id = ?", (session_id or "",)
            ).fetchone()
        if row is None:
            return []
        try:
            return [str(name) for name in json.loads(row["autos"]) if name]
        except (TypeError, ValueError):
            return []

    def pop_pending_attach(self, session_id: str, *, first_turn: bool = False, max_age: float = 600.0) -> list[str]:
        """Take this chat's pending attachments (and delete them).

        A brand-new chat has no stored session id while the user composes its first
        message, so on a first turn with no exact entry the newest entry younger than
        ``max_age`` seconds is taken instead."""
        import json
        import time

        key = session_id or ""
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id, names FROM pending_attach WHERE session_id = ?", (key,)
            ).fetchone()
            if row is None and first_turn:
                row = self._conn.execute(
                    """
                    SELECT session_id, names FROM pending_attach
                     WHERE fresh = 1 AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1
                    """,
                    (time.time() - max_age,),
                ).fetchone()
            if row is None:
                return []
            self._conn.execute("DELETE FROM pending_attach WHERE session_id = ?", (row["session_id"],))
            self._conn.commit()
        try:
            return [str(name) for name in json.loads(row["names"]) if name]
        except (TypeError, ValueError):
            return []

    def clear_pending_forces(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_forces WHERE session_id = ?", (session_id or "",))
            self._conn.commit()

    def link_session(self, session_id: str, parent_id: str) -> None:
        """A compaction continued ``parent_id`` as ``session_id``: they share a fan."""
        if not session_id or not parent_id or session_id == parent_id:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO session_parents (session_id, parent_id) VALUES (?, ?)",
                (session_id, parent_id),
            )
            self._conn.commit()

    def mark_session_compacted(self, session_id: str) -> None:
        if not session_id:
            return
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO session_flags (session_id, compacted, updated_at, compacted_at)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET compacted = 1, updated_at = excluded.updated_at,
                    compacted_at = excluded.compacted_at
                """,
                (session_id, now, now),
            )
            self._conn.commit()

    def clear_session_compacted(self, session_id: str) -> None:
        if not session_id:
            return
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO session_flags (session_id, compacted, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(session_id) DO UPDATE SET compacted = 0, updated_at = excluded.updated_at
                """,
                (session_id, now),
            )
            self._conn.commit()

    def session_compacted(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT compacted FROM session_flags WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return bool(row and row["compacted"])

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()
        return int(row["n"]) if row else 0

    def has_term(self, word: str) -> bool:
        """True when ``word`` (stemmed like the index) occurs anywhere in the catalog."""
        if not word or not word.isalnum() or not self._fts_table_ready():
            return True
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM skills_fts WHERE skills_fts MATCH ? LIMIT 1", (f'"{word}"',)
                ).fetchone()
            except sqlite3.OperationalError:
                return True
        return row is not None

    def doc_frequency(self, word: str) -> int:
        """In how many skills ``word`` occurs (stemmed like the index). Unknown: every skill."""
        total = self.count()
        if not word or not word.isalnum() or not self._fts_table_ready():
            return total
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT count(*) FROM skills_fts WHERE skills_fts MATCH ?", (f'"{word}"',)
                ).fetchone()
            except sqlite3.OperationalError:
                return total
        return int(row[0]) if row else total

    def vocabulary(self) -> frozenset[str]:
        """Words of every skill's name, description and tags (cached with list_skills)."""
        skills = self.list_skills()
        key = (str(self.db_path), len(skills), id(_SKILL_CACHE.get(str(self.db_path))))
        hit = _VOCAB_CACHE.get(key)
        if hit is not None:
            return hit
        words: set[str] = set()
        for skill in skills:
            text = f"{skill.name} {skill.description} {' '.join(skill.tags)}".lower()
            words.update(word for word in re.findall(r"[a-z0-9]+", text) if len(word) >= 4)
        vocab = frozenset(words)
        _VOCAB_CACHE.clear()
        _VOCAB_CACHE[key] = vocab
        return vocab

    def fts_names(self, query: str, limit: int = 50) -> list[str] | None:
        """FTS5 prefilter. ``None`` means FTS is unavailable and the caller should scan."""
        if not self.fts_enabled:
            return None
        match = _fts_query(query)
        if not match:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT name FROM skills_fts
                     WHERE skills_fts MATCH ?
                     ORDER BY rank
                     LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return None
        return [str(row["name"]) for row in rows]

    def fts_bm25(self, query: str, limit: int = 20) -> dict[str, float]:
        """Positive BM25-style strengths over the full skill text. Higher is better.

        Column weights favor ``body`` and ``refs`` over the name and description.
        ``None`` is not used: an empty dict means FTS is off or the query matched nothing.
        """
        if not self.fts_enabled:
            return {}
        match = _fts_query(query)
        if not match:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT name, bm25(skills_fts, 0.4, 0.8, 6.0, 5.0, 0.2) AS score
                      FROM skills_fts
                     WHERE skills_fts MATCH ?
                     ORDER BY score
                     LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
        strengths: dict[str, float] = {}
        for row in rows:
            # FTS5 bm25() is negative; a better match is more negative.
            strengths[str(row["name"])] = max(0.0, -float(row["score"]))
        return strengths

    def bump_use(self, name: str) -> None:
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                UPDATE skills
                   SET use_count = use_count + 1, last_used_at = ?
                 WHERE name = ? COLLATE NOCASE
                """,
                (now, name),
            )
            self._conn.commit()

    def set_embedding(self, name: str, embedding: bytes | None) -> None:
        """Extension point: store an optional embedding blob. The v1 judge does not read it."""
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET embedding = ? WHERE name = ? COLLATE NOCASE",
                (embedding, name),
            )
            self._conn.commit()

    def log_selection(
        self,
        *,
        turn_id: str,
        session_id: str,
        query: str,
        candidates: list[dict[str, Any]],
        chosen: str | None,
        accepted: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO selections (
                    turn_id, session_id, query, candidates_json, chosen, accepted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    query,
                    json.dumps(candidates, ensure_ascii=False),
                    chosen,
                    1 if accepted else 0,
                    _utcnow(),
                ),
            )
            self._conn.commit()

    def recent_selections(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM selections ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "turn_id": row["turn_id"],
                    "session_id": row["session_id"],
                    "query": row["query"],
                    "candidates": json.loads(row["candidates_json"]),
                    "chosen": row["chosen"],
                    "accepted": bool(row["accepted"]),
                    "created_at": row["created_at"],
                }
            )
        return out


# db path -> (catalog signature, skills); see SkillStore.list_skills.
_SKILL_CACHE: dict[str, tuple] = {}
_VOCAB_CACHE: dict[tuple, frozenset] = {}


def _skill_from_row(row: sqlite3.Row) -> Skill:
    try:
        tags = tuple(json.loads(row["tags"] or "[]"))
    except json.JSONDecodeError:
        tags = ()
    return Skill(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"] or ""),
        body=str(row["body"] or ""),
        tags=tuple(str(tag) for tag in tags),
        source=str(row["source"] or ""),
        origin_path=str(row["origin_path"] or ""),
        content_hash=str(row["content_hash"] or ""),
        use_count=int(row["use_count"] or 0),
        last_used_at=row["last_used_at"],
        refs=str(row["refs"] or "") if "refs" in row.keys() else "",
    )


def _fts_query(query: str) -> str:
    tokens = []
    current = []
    for char in query.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            token = "".join(current)
            if len(token) > 1:
                tokens.append('"' + token.replace('"', "") + '"')
            current = []
    if current:
        token = "".join(current)
        if len(token) > 1:
            tokens.append('"' + token.replace('"', "") + '"')
    if not tokens:
        return ""
    return " OR ".join(tokens[:12])
