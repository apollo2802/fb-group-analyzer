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

    def upsert_post(
        self,
        post_id: str,
        author: str,
        content: str,
        timestamp: Optional[str] = None,
        comment_count: int = 0,
        comments: Optional[List[Dict[str, Any]]] = None,
        post_url: Optional[str] = None,
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
                        comment_count, post_url, needs_analysis, dispatched,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                    """,
                    (
                        post_id,
                        author,
                        content,
                        json.dumps(incoming_comments, ensure_ascii=False),
                        timestamp,
                        comment_count,
                        post_url,
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
