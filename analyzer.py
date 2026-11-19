from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Dict, List, Literal, Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from db import StateDB

load_dotenv()


class DerivedActivity(BaseModel):
    is_actionable: bool = Field(
        description="True, wenn eine konkrete Aufgabe, Frage, Anfrage oder ein Termin vorliegt."
    )
    category: Literal["Termin/Event", "Frage/Support", "Bedarf/Anfrage", "To-Do", "Info"]
    action_title: str = Field(
        description="Kurzer, prägnanter Titel der abgeleiteten Aktivität"
    )
    details: str = Field(
        description="Kontext aus Post und Kommentaren (ohne unnötige personenbezogene Daten)"
    )
    due_date: Optional[str] = Field(
        default=None,
        description="ISO-Datum (YYYY-MM-DD), falls zeitlicher Bezug erkennbar, sonst null",
    )
    priority: Literal["Low", "Medium", "High"]
    sentiment: Literal["Positiv", "Neutral", "Kritisch/Negativ"] = Field(
        default="Neutral",
        description="Grundstimmung des Beitrags und der Kommentare: Positiv, Neutral oder Kritisch/Negativ",
    )
    sentiment_score: int = Field(
        default=0,
        ge=-100,
        le=100,
        description="Numerischer Stimmungswert von -100 (stark negativ/frustriert) über 0 (neutral) bis +100 (sehr positiv/begeistert)",
    )


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,5}\)?[\s.-]?)?\d{3,}[\s.-]?\d{2,}")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)


def anonymize_text(text: str, known_names: Optional[List[str]] = None) -> str:
    """Maskiert bekannte Namen und typische Identifikatoren im Freitext."""
    result = text
    result = _EMAIL_RE.sub("[E-MAIL]", result)
    result = _URL_RE.sub("[LINK]", result)
    result = _PHONE_RE.sub("[TELEFON]", result)

    if known_names:
        for idx, name in enumerate(sorted(set(n for n in known_names if n and len(n) >= 3), key=len, reverse=True), start=1):
            result = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", f"[Person_{idx}]", result, flags=re.IGNORECASE)

    return result


def build_prompt(post: Dict) -> str:
    author = post.get("author") or "Unbekannt"
    content = post.get("content") or ""
    timestamp = post.get("timestamp") or "unbekannt"

    try:
        comments = json.loads(post.get("comments") or "[]")
    except json.JSONDecodeError:
        comments = []

    known_names = [author] + [c.get("author", "") for c in comments if isinstance(c, dict)]

    display_content = anonymize_text(content, known_names)
    comment_lines = [
        f"- [Kommentator {i}]: {anonymize_text(c.get('text', ''), known_names)}"
        for i, c in enumerate(comments, start=1)
        if isinstance(c, dict) and c.get("text")
    ]

    comments_block = "\n".join(comment_lines) if comment_lines else "(Keine Kommentare)"

    return (
        f"Heutiges Datum: {date.today().isoformat()}\n\n"
        f"Analysiere den folgenden Beitrag aus einer Facebook-Gruppe samt Kommentaren "
        f"und leite daraus eine strukturierte Aktivität ab.\n\n"
        f"Zeitstempel: {timestamp}\n"
        f"Beitrag:\n{display_content}\n\n"
        f"Kommentare:\n{comments_block}\n"
    )


def analyze_pending_posts(db_path: Optional[str] = None, limit: int = 50, force_all: bool = False) -> int:
    """Analysiert noch nicht ausgewertete (oder bei force_all alle) Posts in state.db via Gemini Structured Outputs."""
    if os.getenv("GEMINI_ALLOW_CLOUD", "").strip().lower() != "true":
        print("[Analyzer] Gemini-Übertragung deaktiviert. Beiträge bleiben zur Analyse vorgemerkt.")
        return 0

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key == "your_gemini_api_key_here":
        raise ValueError("GEMINI_API_KEY ist nicht konfiguriert. Bitte in .env eintragen.")

    model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    db = StateDB(db_path or os.getenv("DB_PATH", "./state.db"))
    pending = db.get_all_posts()[:limit] if force_all else db.get_posts_for_analysis(limit=limit)
    if not pending:
        print("[Analyzer] Keine neuen oder aktualisierten Posts zur Analyse vorhanden.")
        return 0

    client = genai.Client(api_key=api_key)
    analyzed_count = 0

    for post in pending:
        post_id = post["post_id"]
        prompt = build_prompt(post)

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "Du bist ein präziser Assistent zur Auswertung von Facebook-Gruppenbeiträgen. "
                        "Erkenne konkrete Aufgaben, Termine, Fragen oder Bedarfe. "
                        "Reine Smalltalk- oder Info-Beiträge ohne Handlungsbedarf erhalten is_actionable=false. "
                        "Bewerte zusätzlich die Grundstimmung (sentiment und sentiment_score von -100 bis +100) "
                        "basierend auf Tonfall, Lob, Frustration oder gemeldeten Problemen in Post und Kommentaren."
                    ),
                    response_mime_type="application/json",
                    response_schema=DerivedActivity,
                    temperature=0.1,
                ),
            )
            activity = DerivedActivity.model_validate_json(response.text)
            db.save_analysis(post_id, activity.model_dump())
            analyzed_count += 1
            print(
                f"[Analyzer] Post {post_id}: actionable={activity.is_actionable} | "
                f"{activity.category} | {activity.priority} | "
                f"{activity.sentiment} ({activity.sentiment_score:+d}) | {activity.action_title}"
            )
        except Exception as exc:
            print(f"[Analyzer] Fehler bei Post {post_id}: {exc}")

    print(f"[Analyzer] Fertig: {analyzed_count}/{len(pending)} Posts analysiert.")
    return analyzed_count


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="KI-Analyse mit Gemini Structured Outputs")
    parser.add_argument("--reanalyze-all", action="store_true", help="Analysiert alle vorhandenen Posts neu (z.B. nach Schema-Erweiterung)")
    args = parser.parse_args()
    analyze_pending_posts(force_all=args.reanalyze_all)
