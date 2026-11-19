import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class StateDB:
    """SQLite-Datenbank zur Deduplizierung von Facebook-Posts und Speicherung abgeleiteter Aktivitäten."""

    def __init__(self, db_path: str = "./state.db") -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
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
                    status TEXT DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Migration für bestehende Datenbanken ohne status-Spalte
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(posts)").fetchall()
            }
            if "status" not in cols:
                conn.execute("ALTER TABLE posts ADD COLUMN status TEXT DEFAULT 'open'")
            conn.commit()

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
        comments_json = json.dumps(comments or [], ensure_ascii=False)

        with self._connect() as conn:
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
                        comments_json,
                        timestamp,
                        comment_count,
                        post_url,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True

            # Nur neu analysieren, wenn neue Kommentare oder neuer Inhalt dazugekommen sind
            prev_count = row["comment_count"] or 0
            prev_content = row["content"] or ""
            prev_comments = row["comments"] or "[]"

            has_changes = (
                comment_count > prev_count
                or (content and content != prev_content)
                or (comments and comments_json != prev_comments)
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
                        comments_json,
                        timestamp,
                        comment_count,
                        post_url,
                        now,
                        post_id,
                    ),
                )
                conn.commit()
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
                    updated_at = ?
                WHERE post_id = ?
                """,
                (json.dumps(extracted_actions, ensure_ascii=False), now, post_id),
            )
            conn.commit()

    def get_undispatched_posts(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM posts
                WHERE extracted_actions IS NOT NULL AND dispatched = 0
                ORDER BY updated_at ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_dispatched(self, post_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE posts SET dispatched = 1, updated_at = ? WHERE post_id = ?",
                (now, post_id),
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
                "SELECT COUNT(*) FROM posts WHERE extracted_actions IS NOT NULL AND dispatched = 0"
            ).fetchone()[0]
            return {
                "total_posts": total,
                "pending_analysis": pending,
                "undispatched": undispatched,
            }
