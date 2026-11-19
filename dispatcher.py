from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _export_key(row: Dict[str, Any]) -> tuple[str, str]:
    return row["post_id"], row["dispatched_at"]


def _export_csv(db: StateDB, csv_path: Path, records: List[Dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    # Scheduler und Dashboard können dieselbe Pipeline gleichzeitig starten.
    with db.write_transaction():
        has_csv = csv_path.exists() and csv_path.stat().st_size > 0
        existing_keys = set()
        if has_csv:
            with csv_path.open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                if reader.fieldnames != CSV_COLUMNS:
                    raise ValueError(f"Unerwartete CSV-Spalten in {csv_path}")
                existing_keys = {_export_key(row) for row in reader}
        new_records = [row for row in records if _export_key(row) not in existing_keys]
        if not new_records:
            return

        # Die vorhandene Historie bleibt auch bei einem Abbruch beim Schreiben intakt.
        temporary_file = tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=csv_path.parent, delete=False,
        )
        temporary_path = Path(temporary_file.name)
        try:
            with temporary_file as output:
                writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
                if has_csv:
                    with csv_path.open(newline="", encoding="utf-8") as source:
                        shutil.copyfileobj(source, output)
                else:
                    writer.writeheader()
                writer.writerows(new_records)
                output.flush()
                os.fsync(output.fileno())
            temporary_path.replace(csv_path)
        finally:
            temporary_path.unlink(missing_ok=True)


def _send_webhook(webhook_url: str, payload: Dict[str, Any]) -> None:
    delivery_key = hashlib.sha256(json.dumps(_export_key(payload)).encode()).hexdigest()
    response = httpx.post(
        webhook_url, json=payload, headers={"Idempotency-Key": delivery_key}, timeout=15.0,
    )
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

    records = []
    for post in posts:
        post_id = post["post_id"]
        try:
            action = json.loads(post["extracted_actions"])
        except (TypeError, json.JSONDecodeError) as exc:
            print(f"[Dispatcher] Ungültiges JSON für Post {post_id}: {exc}")
            continue

        records.append({
            "dispatched_at": post["export_started_at"],
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
        })

    if not records:
        return 0
    _export_csv(db, csv_path, records)

    dispatched_count = 0
    for record in records:
        post_id, export_started_at = _export_key(record)
        try:
            if webhook and (not only_actionable_to_webhook or record["is_actionable"]):
                _send_webhook(webhook, record)

            db.mark_dispatched(post_id, export_started_at)
            dispatched_count += 1
        except Exception as exc:
            print(f"[Dispatcher] Fehler beim Weiterleiten von Post {post_id}: {exc}")

    print(f"[Dispatcher] Fertig: {dispatched_count}/{len(records)} Exporte abgeschlossen ({csv_path}).")
    return dispatched_count


if __name__ == "__main__":
    dispatch_activities()
