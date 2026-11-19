from __future__ import annotations

import argparse
import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from playwright.sync_api import Response, sync_playwright

from db import StateDB

load_dotenv()


def _parse_ndjson_payload(raw_text: str) -> List[Dict[str, Any]]:
    """Parst Facebook-GraphQL-Antworten (oft NDJSON mit optionalem 'for (;;);'-Prefix)."""
    cleaned = raw_text.strip()
    if cleaned.startswith("for (;;);"):
        cleaned = cleaned[len("for (;;);") :].strip()

    objects: List[Dict[str, Any]] = []
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                objects.append(parsed)
        except json.JSONDecodeError:
            continue
    return objects


def _iter_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    """Durchläuft verschachtelte JSON-Strukturen rekursiv und liefert alle Dicts."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _find_first_text(node: Any, keys: tuple[str, ...] = ("text",)) -> Optional[str]:
    for d in _iter_dicts(node):
        for key in keys:
            val = d.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _extract_comments_from_story(story: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extrahiert sichtbare Kommentare aus einem GraphQL-Story-Objekt."""
    comments: List[Dict[str, str]] = []
    seen_texts: set[str] = set()

    for d in _iter_dicts(story):
        if d.get("__typename") == "Comment" or (
            "body" in d and isinstance(d.get("body"), dict) and "author" in d
        ):
            body_text = d.get("body", {}).get("text")
            if not isinstance(body_text, str) or not body_text.strip():
                continue
            body_text = body_text.strip()
            if body_text in seen_texts:
                continue
            seen_texts.add(body_text)

            author_obj = d.get("author") or {}
            author_name = (
                author_obj.get("name", "Unbekannt")
                if isinstance(author_obj, dict)
                else "Unbekannt"
            )
            comments.append({"author": str(author_name), "text": body_text})

    return comments


def _extract_comment_count(story: Dict[str, Any]) -> int:
    """Ermittelt die höchste gefundene Kommentaranzahl im Story-Objekt."""
    counts: List[int] = []
    for d in _iter_dicts(story):
        for key in ("total_comment_count", "comment_count"):
            val = d.get(key)
            if isinstance(val, int):
                counts.append(val)
            elif isinstance(val, dict) and isinstance(val.get("total_count"), int):
                counts.append(val["total_count"])
        comments_obj = d.get("comments")
        if isinstance(comments_obj, dict) and isinstance(comments_obj.get("total_count"), int):
            counts.append(comments_obj["total_count"])
    return max(counts) if counts else 0


def _extract_timestamp(story: Dict[str, Any]) -> Optional[str]:
    for d in _iter_dicts(story):
        ts = d.get("creation_time")
        if isinstance(ts, (int, float)) and ts > 1_000_000_000:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return None


def _extract_author(story: Dict[str, Any]) -> str:
    actors = story.get("actors")
    if isinstance(actors, list) and actors:
        first = actors[0]
        if isinstance(first, dict) and first.get("name"):
            return str(first["name"])
    for d in _iter_dicts(story):
        actors = d.get("actors")
        if isinstance(actors, list) and actors:
            first = actors[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"])
    return "Unbekannt"


def _extract_message(story: Dict[str, Any]) -> str:
    # Direktes message-Feld bevorzugen
    msg = story.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
        return msg["text"].strip()

    # In comet_sections nach message.text suchen
    comet = story.get("comet_sections")
    if isinstance(comet, dict):
        for d in _iter_dicts(comet):
            m = d.get("message")
            if isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"].strip():
                return m["text"].strip()

    return ""


def _extract_post_url(story: Dict[str, Any]) -> Optional[str]:
    for key in ("wwwURL", "url"):
        val = story.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    for d in _iter_dicts(story):
        val = d.get("wwwURL")
        if isinstance(val, str) and "/posts/" in val or (isinstance(val, str) and "/permalink/" in val):
            return val
    return None


def extract_posts_from_graphql(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extrahiert normalisierte Post-Datensätze aus einer geparsten GraphQL-Antwort."""
    posts: Dict[str, Dict[str, Any]] = {}

    for node in _iter_dicts(payload):
        post_id = node.get("post_id")
        if not post_id:
            continue

        # Nur Story-ähnliche Objekte berücksichtigen
        is_story = (
            node.get("__typename") == "Story"
            or "comet_sections" in node
            or "message" in node
            or "actors" in node
        )
        if not is_story:
            continue

        pid = str(post_id)
        content = _extract_message(node)
        author = _extract_author(node)
        comments = _extract_comments_from_story(node)
        comment_count = max(_extract_comment_count(node), len(comments))
        timestamp = _extract_timestamp(node)
        post_url = _extract_post_url(node)

        if not content and not comments:
            continue

        existing = posts.get(pid)
        if existing is None or len(content) > len(existing["content"]) or len(comments) > len(existing["comments"]):
            posts[pid] = {
                "post_id": pid,
                "author": author,
                "content": content,
                "comments": comments,
                "comment_count": comment_count,
                "timestamp": timestamp,
                "post_url": post_url,
            }

    return list(posts.values())


def scrape_group(
    group_url: Optional[str] = None,
    session_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    headless: Optional[bool] = None,
    scroll_count: Optional[int] = None,
    scroll_delay: Optional[float] = None,
    debug_graphql: bool = False,
) -> Dict[str, int]:
    """
    Öffnet die Facebook-Gruppe mit dem gespeicherten Playwright-Profil,
    scrollt langsam nach unten, fängt /api/graphql/-Antworten ab und speichert
    neue oder aktualisierte Posts in state.db.
    """
    group_url = (group_url or os.getenv("FB_GROUP_URL", "")).strip()
    if not group_url or "DEINE_GRUPPE" in group_url:
        raise ValueError("FB_GROUP_URL ist nicht konfiguriert. Bitte in .env eintragen.")

    # Chronologische Sortierung erzwingen, damit brandneue Beiträge immer ganz oben erscheinen
    if "sorting_setting=" not in group_url:
        sep = "&" if "?" in group_url else "?"
        group_url = f"{group_url}{sep}sorting_setting=CHRONOLOGICAL"

    session_path = Path(session_dir or os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
    if not session_path.exists():
        raise FileNotFoundError(
            f"Session-Ordner {session_path} existiert nicht. Bitte zuerst 'python auth_setup.py' ausführen."
        )

    db = StateDB(db_path or os.getenv("DB_PATH", "./state.db"))
    if headless is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
    scroll_count = scroll_count if scroll_count is not None else int(os.getenv("SCROLL_COUNT", "4"))
    scroll_delay = scroll_delay if scroll_delay is not None else float(os.getenv("SCROLL_DELAY_SECONDS", "3.0"))

    debug_dir = Path("./debug_graphql")
    if debug_graphql:
        debug_dir.mkdir(parents=True, exist_ok=True)

    discovered_posts: Dict[str, Dict[str, Any]] = {}
    graphql_responses_seen = 0

    def merge_post(post: Dict[str, Any]) -> None:
        pid = post["post_id"]
        prev = discovered_posts.get(pid)
        if (
            prev is None
            or len(post["content"]) > len(prev["content"])
            or post["comment_count"] > prev["comment_count"]
            or len(post["comments"]) > len(prev["comments"])
        ):
            discovered_posts[pid] = post

    def handle_response(response: Response) -> None:
        nonlocal graphql_responses_seen
        if "/api/graphql/" not in response.url:
            return
        if response.status != 200:
            return

        try:
            body = response.text()
        except Exception:
            return

        graphql_responses_seen += 1
        if debug_graphql:
            dump_file = debug_dir / f"graphql_{graphql_responses_seen:03d}.txt"
            dump_file.write_text(body, encoding="utf-8")

        for payload in _parse_ndjson_payload(body):
            for post in extract_posts_from_graphql(payload):
                merge_post(post)

    print(f"[Scraper] Starte Browser (headless={headless}) für {group_url} ...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(session_path),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", handle_response)

        try:
            page.goto(group_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4000)

            # Prüfen, ob Login-Seite angezeigt wird
            if "login" in page.url.lower():
                raise RuntimeError(
                    "Facebook verlangt einen erneuten Login. Bitte 'python auth_setup.py' erneut ausführen."
                )

            # Initial serverseitig gerenderte (SSR) JSON-Skripte auslesen (enthält die obersten/neuesten Posts!)
            try:
                ssr_scripts = page.locator('script[type="application/json"]').all_text_contents()
                for raw_json in ssr_scripts:
                    if "post_id" not in raw_json:
                        continue
                    try:
                        payload = json.loads(raw_json)
                        if isinstance(payload, dict):
                            for post in extract_posts_from_graphql(payload):
                                merge_post(post)
                    except json.JSONDecodeError:
                        continue
            except Exception as exc:
                print(f"[Scraper] Hinweis beim SSR-JSON-Parsing: {exc}")

            for i in range(scroll_count):
                scroll_px = random.randint(900, 1400)
                page.mouse.wheel(0, scroll_px)
                delay = scroll_delay + random.uniform(0.5, 1.8)
                print(f"[Scraper] Scroll {i + 1}/{scroll_count} ({scroll_px}px), warte {delay:.1f}s ...")
                time.sleep(delay)

            # Kurz warten, damit laufende GraphQL-Requests abgeschlossen werden
            page.wait_for_timeout(2500)
        finally:
            context.close()

    new_or_updated = 0
    for post in discovered_posts.values():
        changed = db.upsert_post(
            post_id=post["post_id"],
            author=post["author"],
            content=post["content"],
            timestamp=post["timestamp"],
            comment_count=post["comment_count"],
            comments=post["comments"],
            post_url=post["post_url"],
        )
        if changed:
            new_or_updated += 1

    stats = {
        "graphql_responses": graphql_responses_seen,
        "posts_extracted": len(discovered_posts),
        "new_or_updated": new_or_updated,
    }
    print(
        f"[Scraper] Fertig: {stats['graphql_responses']} GraphQL-Antworten, "
        f"{stats['posts_extracted']} Posts extrahiert, {stats['new_or_updated']} neu/aktualisiert."
    )
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook-Gruppen-Scraper via GraphQL-Interception")
    parser.add_argument("--debug-graphql", action="store_true", help="Speichert rohe GraphQL-Responses in ./debug_graphql/")
    parser.add_argument("--visible", action="store_true", help="Startet den Browser sichtbar (headless=False)")
    args = parser.parse_args()

    scrape_group(
        headless=False if args.visible else None,
        debug_graphql=args.debug_graphql,
    )
