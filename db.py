import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class StateDB:
    """SQLite-Datenbank zur Deduplizierung von Facebook-Posts und Speicherung abgeleiteter Aktivitäten."""

    def __init__(self, db_path: str = "./state.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialisiert Schreibzugriffe über mehrere Pipeline-Prozesse hinweg."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.write_transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY,
                    author TEXT,
                    content TEXT,
                    comments TEXT DEFAULT '[]',
                    timestamp TEXT,
                    comment_count INTEGER DEFAULT 0,
                    post_url TEXT,
                    extracted_actions TEXT,
                    needs_analysis INTEGER DEFAULT 1,
                    dispatched INTEGER DEFAULT 0,
                    export_started_at TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(posts)").fetchall()
            }
            if "status" not in cols:
                conn.execute("ALTER TABLE posts ADD COLUMN status TEXT DEFAULT 'open'")
            if "export_started_at" not in cols:
                conn.execute("ALTER TABLE posts ADD COLUMN export_started_at TEXT")
            if "group_url" not in cols:
                conn.execute("ALTER TABLE posts ADD COLUMN group_url TEXT")
            if "group_name" not in cols:
                conn.execute("ALTER TABLE posts ADD COLUMN group_name TEXT")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    enabled INTEGER DEFAULT 1,
                    last_scraped_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def set_runtime_state(self, updates: Dict[str, str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.write_transaction() as conn:
            for key, value in updates.items():
                conn.execute(
                    """
                    INSERT INTO runtime_state (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, value, now),
                )

    def get_runtime_state(self) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM runtime_state").fetchall()
            return {r["key"]: r["value"] for r in rows}

    @staticmethod
    def normalize_group_url(url: str) -> str:
        cleaned = url.strip()
        if "?" in cleaned:
            cleaned = cleaned.split("?", 1)[0]
        return cleaned.rstrip("/")

    def ensure_default_group(self, default_url: str, default_name: str = "Standard-Gruppe") -> None:
        """Legt FB_GROUP_URL aus .env einmalig als erste Gruppe an, falls noch keine Gruppen existieren."""
        cleaned = self.normalize_group_url(default_url)
        if not cleaned or "DEINE_GRUPPE" in cleaned:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self.write_transaction() as conn:
            count = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
            if count == 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO groups (name, url, enabled, created_at)
                    VALUES (?, ?, 1, ?)
                    """,
                    (default_name, cleaned, now),
                )
                conn.execute(
                    """
                    UPDATE posts
                    SET group_url = ?, group_name = ?
                    WHERE group_url IS NULL
                    """,
                    (cleaned, default_name),
                )

    def get_groups(self, only_enabled: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if only_enabled:
                rows = conn.execute(
                    "SELECT * FROM groups WHERE enabled = 1 ORDER BY id ASC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM groups ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def add_group(self, name: str, url: str) -> int:
        cleaned_url = self.normalize_group_url(url)
        cleaned_name = name.strip() or cleaned_url.rsplit("/", 1)[-1]
        if not cleaned_url.startswith("http"):
            raise ValueError("Die Gruppen-URL muss mit http:// oder https:// beginnen.")
        now = datetime.now(timezone.utc).isoformat()
        with self.write_transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO groups (name, url, enabled, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (cleaned_name, cleaned_url, now),
            )
            return int(cur.lastrowid)

    def update_group(
        self,
        group_id: int,
        name: Optional[str] = None,
        url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        with self.write_transaction() as conn:
            row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
            if row is None:
                return
            new_name = name.strip() if name is not None and name.strip() else row["name"]
            new_url = self.normalize_group_url(url) if url is not None and url.strip() else row["url"]
            new_enabled = int(enabled) if enabled is not None else row["enabled"]
            conn.execute(
                "UPDATE groups SET name = ?, url = ?, enabled = ? WHERE id = ?",
                (new_name, new_url, new_enabled, group_id),
            )
            if new_name != row["name"] or new_url != row["url"]:
                conn.execute(
                    "UPDATE posts SET group_name = ?, group_url = ? WHERE group_url = ?",
                    (new_name, new_url, row["url"]),
                )

    def delete_group(self, group_id: int) -> None:
        with self.write_transaction() as conn:
            conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def mark_group_scraped(self, url: str) -> None:
        cleaned = self.normalize_group_url(url)
        now = datetime.now(timezone.utc).isoformat()
        with self.write_transaction() as conn:
            conn.execute(
                "UPDATE groups SET last_scraped_at = ? WHERE url = ?",
                (now, cleaned),
            )

    def upsert_post(
        self,
        post_id: str,
        author: str,
        content: str,
        timestamp: Optional[str] = None,
        comment_count: int = 0,
        comments: Optional[List[Dict[str, Any]]] = None,
        post_url: Optional[str] = None,
        group_url: Optional[str] = None,
        group_name: Optional[str] = None,
    ) -> bool:
        """
        Speichert einen neuen Post oder aktualisiert einen bestehenden Post,
        falls sich die Kommentaranzahl oder der Inhalt geändert hat.
        Gibt True zurück, wenn der Post neu ist oder aktualisiert wurde.
        """
        now = datetime.now(timezone.utc).isoformat()
        incoming_comments = comments or []

        with self.write_transaction() as conn:
            row = conn.execute(
                "SELECT content, comment_count, comments FROM posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO posts (
                        post_id, author, content, comments, timestamp,
                        comment_count, post_url, group_url, group_name,
                        needs_analysis, dispatched, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                    """,
                    (
                        post_id,
                        author,
                        content,
                        json.dumps(incoming_comments, ensure_ascii=False),
                        timestamp,
                        comment_count,
                        post_url,
                        self.normalize_group_url(group_url) if group_url else None,
                        group_name,
                        now,
                        now,
                    ),
                )
                return True

            prev_count = row["comment_count"] or 0
            prev_content = row["content"] or ""
            stored_comments = json.loads(row["comments"] or "[]")
            # Facebook liefert Ausschnitte. Fehlende Kommentare in einer Antwort
            # sind kein Nachweis dafür, dass sie gelöscht wurden.
            merged_comments = stored_comments.copy()
            for comment in incoming_comments:
                if comment not in merged_comments:
                    merged_comments.append(comment)

            has_changes = (
                comment_count > prev_count
                or (content and content != prev_content)
                or merged_comments != stored_comments
            )

            if has_changes:
                conn.execute(
                    """
                    UPDATE posts
                    SET author = COALESCE(NULLIF(?, ''), author),
                        content = COALESCE(NULLIF(?, ''), content),
                        comments = ?,
                        timestamp = COALESCE(?, timestamp),
                        comment_count = MAX(comment_count, ?),
                        post_url = COALESCE(?, post_url),
                        group_url = COALESCE(?, group_url),
                        group_name = COALESCE(?, group_name),
                        needs_analysis = 1,
                        updated_at = ?
                    WHERE post_id = ?
                    """,
                    (
                        author,
                        content,
                        json.dumps(merged_comments, ensure_ascii=False),
                        timestamp,
                        comment_count,
                        post_url,
                        self.normalize_group_url(group_url) if group_url else None,
                        group_name,
                        now,
                        post_id,
                    ),
                )
                return True

            return False

    def get_posts_for_analysis(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM posts
                WHERE needs_analysis = 1
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_analysis(self, post_id: str, extracted_actions: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE posts
                SET extracted_actions = ?,
                    needs_analysis = 0,
                    dispatched = 0,
                    export_started_at = NULL,
                    updated_at = ?
                WHERE post_id = ?
                """,
                (json.dumps(extracted_actions, ensure_ascii=False), now, post_id),
            )
            conn.commit()

    def get_undispatched_posts(self) -> List[Dict[str, Any]]:
        with self.write_transaction() as conn:
            conn.execute(
                """
                UPDATE posts SET export_started_at = ?
                WHERE needs_analysis = 0 AND extracted_actions IS NOT NULL
                  AND dispatched = 0 AND export_started_at IS NULL
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
            rows = conn.execute(
                """
                SELECT * FROM posts
                WHERE needs_analysis = 0 AND extracted_actions IS NOT NULL AND dispatched = 0
                ORDER BY updated_at ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_dispatched(self, post_id: str, export_started_at: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE posts SET dispatched = 1, updated_at = ? WHERE post_id = ? AND export_started_at = ?",
                (now, post_id, export_started_at),
            )
            conn.commit()

    def get_all_posts(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM posts
                ORDER BY COALESCE(timestamp, created_at) DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_recent_posts_for_group(
        self,
        group_url: str,
        hours: int = 72,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Liefert die neuesten bekannten Posts einer Gruppe mit Permalink-URL für den Kommentar-Check."""
        cleaned = self.normalize_group_url(group_url)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM posts
                WHERE group_url = ?
                  AND post_url IS NOT NULL
                  AND post_url != ''
                  AND datetime(COALESCE(timestamp, created_at)) >= datetime('now', ?)
                ORDER BY COALESCE(timestamp, created_at) DESC
                LIMIT ?
                """,
                (cleaned, f"-{int(hours)} hours", limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_status(self, post_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE posts SET status = ?, updated_at = ? WHERE post_id = ?",
                (status, now, post_id),
            )
            conn.commit()

    def get_stats(self) -> Dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE needs_analysis = 1"
            ).fetchone()[0]
            undispatched = conn.execute(
                "SELECT COUNT(*) FROM posts WHERE needs_analysis = 0 AND extracted_actions IS NOT NULL AND dispatched = 0"
            ).fetchone()[0]
            return {
                "total_posts": total,
                "pending_analysis": pending,
                "undispatched": undispatched,
            }
