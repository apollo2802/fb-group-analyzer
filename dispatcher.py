from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv

from db import StateDB

load_dotenv()

CSV_COLUMNS = [
    "dispatched_at",
    "post_id",
    "post_timestamp",
    "post_url",
    "is_actionable",
    "category",
    "priority",
    "sentiment",
    "sentiment_score",
    "due_date",
    "action_title",
    "details",
]


def _append_to_csv(csv_path: Path, row: Dict[str, Any]) -> None:
    file_exists = csv_path.exists()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})


def _send_webhook(webhook_url: str, payload: Dict[str, Any]) -> None:
    response = httpx.post(webhook_url, json=payload, timeout=15.0)
    response.raise_for_status()


def dispatch_activities(
    db_path: Optional[str] = None,
    csv_output_path: Optional[str] = None,
    webhook_url: Optional[str] = None,
    only_actionable_to_webhook: bool = True,
) -> int:
    """Leitet analysierte Aktivitäten an CSV und optional an einen Webhook weiter."""
    db = StateDB(db_path or os.getenv("DB_PATH", "./state.db"))
    csv_path = Path(csv_output_path or os.getenv("CSV_OUTPUT_PATH", "./activities.csv"))
    webhook = (webhook_url if webhook_url is not None else os.getenv("WEBHOOK_URL", "")).strip()

    posts = db.get_undispatched_posts()
    if not posts:
        print("[Dispatcher] Keine neuen Aktivitäten zum Weiterleiten vorhanden.")
        return 0

    dispatched_count = 0
    for post in posts:
        post_id = post["post_id"]
        try:
            action = json.loads(post["extracted_actions"])
        except (TypeError, json.JSONDecodeError) as exc:
            print(f"[Dispatcher] Ungültiges JSON für Post {post_id}: {exc}")
            continue

        record = {
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
            "post_id": post_id,
            "post_timestamp": post.get("timestamp") or "",
            "post_url": post.get("post_url") or "",
            "is_actionable": action.get("is_actionable", False),
            "category": action.get("category", "Info"),
            "priority": action.get("priority", "Low"),
            "sentiment": action.get("sentiment", "Neutral"),
            "sentiment_score": action.get("sentiment_score", 0),
            "due_date": action.get("due_date") or "",
            "action_title": action.get("action_title", ""),
            "details": action.get("details", ""),
        }

        try:
            _append_to_csv(csv_path, record)

            if webhook and (not only_actionable_to_webhook or record["is_actionable"]):
                _send_webhook(webhook, record)

            db.mark_dispatched(post_id)
            dispatched_count += 1
        except Exception as exc:
            print(f"[Dispatcher] Fehler beim Weiterleiten von Post {post_id}: {exc}")

    print(f"[Dispatcher] Fertig: {dispatched_count} Einträge nach {csv_path} exportiert.")
    return dispatched_count


if __name__ == "__main__":
    dispatch_activities()
