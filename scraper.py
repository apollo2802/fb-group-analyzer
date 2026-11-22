from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx
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


def _notify_session_expired(db: StateDB, reason: str) -> None:
    """Speichert Session-Ablauf in state.db und sendet einmalig macOS-Notification + optional Webhook."""
    prev_state = db.get_runtime_state()
    already_notified = prev_state.get("fb_session_status") == "expired"
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    msg = f"{reason} (erkannt: {now_str})"

    db.set_runtime_state(
        {
            "fb_session_status": "expired",
            "fb_session_message": msg,
        }
    )

    if already_notified:
        return

    # 1. Native macOS-Benachrichtigung
    if platform.system() == "Darwin":
        try:
            safe_msg = msg.replace('"', "'")
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_msg}" with title "Facebook Group Analyzer" subtitle "Login abgelaufen"',
                ],
                check=False,
                timeout=5,
            )
        except Exception:
            pass

    # 2. Optional Webhook benachrichtigen
    webhook = os.getenv("WEBHOOK_URL", "").strip()
    if webhook:
        try:
            httpx.post(
                webhook,
                json={
                    "event": "session_expired",
                    "title": "Facebook-Session abgelaufen",
                    "message": msg,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                timeout=10.0,
            )
        except Exception:
            pass


def _load_saved_cookies(context: Any, session_path: Path) -> None:
    """Lädt gespeicherte Cookies aus storage_state.json in den Headless-Context (umgeht macOS-Keychain-Mismatch)."""
    state_file = session_path.parent / "storage_state.json"
    if not state_file.exists():
        return
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        if cookies:
            context.add_cookies(cookies)
    except Exception as exc:
        print(f"[Scraper] Hinweis beim Laden von storage_state.json: {exc}")


def _is_facebook_logged_in(context: Any, page_url: str) -> tuple[bool, str]:
    """Prüft zuverlässig anhand des Facebook-Cookies 'c_user' und der URL, ob die Session aktiv ist."""
    lower_url = (page_url or "").lower()
    if any(marker in lower_url for marker in ("/login", "/checkpoint", "two_step_verification", "recover")):
        return False, "Facebook leitet auf Login/Checkpoint um"

    try:
        cookies = context.cookies("https://www.facebook.com")
        has_c_user = any(c.get("name") == "c_user" and c.get("value") for c in cookies)
        if not has_c_user:
            return False, "Facebook-Session abgelaufen (Login-Cookie c_user fehlt)"
    except Exception:
        pass

    return True, ""


def _fetch_comments_for_post(page: Any, post_url: str) -> List[Dict[str, str]]:
    """Öffnet den Permalink eines Beitrags und sammelt alle geladenen Kommentare aus GraphQL & SSR."""
    comments: List[Dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def collect_from_payload(payload: Dict[str, Any]) -> None:
        for c in _extract_comments_from_story(payload):
            key = (c.get("author", ""), c.get("text", ""))
            if key not in seen_pairs:
                seen_pairs.add(key)
                comments.append(c)

    def handle_resp(response: Response) -> None:
        if "/api/graphql/" not in response.url or response.status != 200:
            return
        try:
            body = response.text()
        except Exception:
            return
        for payload in _parse_ndjson_payload(body):
            collect_from_payload(payload)

    page.on("response", handle_resp)
    try:
        page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(3500)
        # Kurz nach unten scrollen, damit nachgeladene Kommentare per GraphQL ausgelöst werden
        try:
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(1200)
        except Exception:
            pass

        # Zugeklappte Antworten auf Kommentare ("X Antworten ansehen/anzeigen", "Weitere Kommentare") aufklappen
        reply_btn_pattern = re.compile(
            r"(Antwort(?:en)?\s+(?:anzeigen|ansehen)|Weitere\s+Kommentare|Alle\s+\d+\s+Antworten|"
            r"hat\s+geantwortet|haben\s+geantwortet|View\s+.*repl|View\s+more\s+comments|replied)",
            re.IGNORECASE,
        )
        try:
            expand_buttons = page.locator('[role="button"]').filter(has_text=reply_btn_pattern)
            clicked = 0
            btn_count = min(expand_buttons.count(), 8)
            for idx in range(btn_count):
                if clicked >= 6:
                    break
                btn = expand_buttons.nth(idx)
                try:
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        clicked += 1
                        page.wait_for_timeout(900)
                except Exception:
                    continue
            if clicked > 0:
                page.wait_for_timeout(1200)
        except Exception:
            pass

        try:
            for raw_json in page.locator('script[type="application/json"]').all_text_contents():
                if "Comment" not in raw_json and '"body"' not in raw_json:
                    continue
                try:
                    payload = json.loads(raw_json)
                    if isinstance(payload, dict):
                        collect_from_payload(payload)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
    except Exception as exc:
        print(f"[Scraper] Hinweis beim Laden der Kommentare ({post_url}): {exc}")
    finally:
        page.remove_listener("response", handle_resp)

    return comments


def _scrape_single_page(
    page: Any,
    group_url: str,
    group_name: str,
    db: StateDB,
    scroll_count: int,
    scroll_delay: float,
    debug_graphql: bool = False,
    debug_prefix: str = "graphql",
    fetch_comments: bool = True,
    max_comment_posts: int = 8,
) -> Dict[str, int]:
    base_url = db.normalize_group_url(group_url)
    target_url = group_url.strip()
    if "sorting_setting=" not in target_url:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}sorting_setting=CHRONOLOGICAL"

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
            dump_file = debug_dir / f"{debug_prefix}_{graphql_responses_seen:03d}.txt"
            dump_file.write_text(body, encoding="utf-8")

        for payload in _parse_ndjson_payload(body):
            for post in extract_posts_from_graphql(payload):
                merge_post(post)

    print(f"[Scraper] Lade Gruppe '{group_name}' ({target_url}) ...")
    page.on("response", handle_response)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        logged_in, reason = _is_facebook_logged_in(page.context, page.url)
        if not logged_in:
            _notify_session_expired(db, reason)
            raise RuntimeError(
                f"{reason}. Bitte im Dashboard auf 'Login' klicken, um die Session zu erneuern."
            )

        db.set_runtime_state(
            {
                "fb_session_status": "ok",
                "fb_session_message": f"Aktiv (geprüft: {datetime.now().strftime('%d.%m.%Y %H:%M')})",
            }
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
            print(f"[Scraper] [{group_name}] Scroll {i + 1}/{scroll_count} ({scroll_px}px), warte {delay:.1f}s ...")
            time.sleep(delay)

        page.wait_for_timeout(2500)
    finally:
        page.remove_listener("response", handle_response)

    # Bei großen Gruppen liefert der Feed oft keine Kommentare inline mit:
    # gezielt die Permalinks der neu gefundenen UND der jüngsten bekannten Beiträge (72h) öffnen.
    if fetch_comments and max_comment_posts > 0:
        candidates: Dict[str, Dict[str, Any]] = {}
        for post in discovered_posts.values():
            if post.get("post_url"):
                candidates[post["post_id"]] = post

        for db_post in db.get_recent_posts_for_group(base_url, hours=72, limit=max_comment_posts):
            pid = db_post["post_id"]
            if pid not in candidates and db_post.get("post_url"):
                try:
                    stored_comments = json.loads(db_post.get("comments") or "[]")
                except json.JSONDecodeError:
                    stored_comments = []
                candidates[pid] = {
                    "post_id": pid,
                    "author": db_post.get("author") or "Unbekannt",
                    "content": db_post.get("content") or "",
                    "comments": stored_comments,
                    "comment_count": db_post.get("comment_count") or 0,
                    "timestamp": db_post.get("timestamp"),
                    "post_url": db_post.get("post_url"),
                }

        posts_to_check = sorted(
            candidates.values(),
            key=lambda p: p.get("timestamp") or "",
            reverse=True,
        )[:max_comment_posts]

        for post in posts_to_check:
            fetched = _fetch_comments_for_post(page, post["post_url"])
            if fetched:
                existing_comments = post.get("comments") or []
                merged = list(existing_comments)
                for c in fetched:
                    if c not in merged:
                        merged.append(c)
                post["comments"] = merged
                post["comment_count"] = max(post.get("comment_count") or 0, len(merged))
                discovered_posts[post["post_id"]] = post
            time.sleep(random.uniform(1.0, 2.2))

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
            group_url=base_url,
            group_name=group_name,
        )
        if changed:
            new_or_updated += 1

    db.mark_group_scraped(base_url)

    stats = {
        "graphql_responses": graphql_responses_seen,
        "posts_extracted": len(discovered_posts),
        "new_or_updated": new_or_updated,
    }
    print(
        f"[Scraper] [{group_name}] Fertig: {stats['graphql_responses']} GraphQL-Antworten, "
        f"{stats['posts_extracted']} Posts extrahiert, {stats['new_or_updated']} neu/aktualisiert."
    )
    return stats


def scrape_group(
    group_url: Optional[str] = None,
    group_name: Optional[str] = None,
    session_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    headless: Optional[bool] = None,
    scroll_count: Optional[int] = None,
    scroll_delay: Optional[float] = None,
    debug_graphql: bool = False,
) -> Dict[str, int]:
    """Scrapt eine einzelne Facebook-Gruppe."""
    group_url = (group_url or os.getenv("FB_GROUP_URL", "")).strip()
    if not group_url or "DEINE_GRUPPE" in group_url:
        raise ValueError("FB_GROUP_URL ist nicht konfiguriert. Bitte in .env oder im Dashboard eintragen.")

    session_path = Path(session_dir or os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
    if not session_path.exists():
        raise FileNotFoundError(
            f"Session-Ordner {session_path} existiert nicht. Bitte zuerst Login durchführen."
        )

    db = StateDB(db_path or os.getenv("DB_PATH", "./state.db"))
    if headless is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
    scroll_count = scroll_count if scroll_count is not None else int(os.getenv("SCROLL_COUNT", "4"))
    scroll_delay = scroll_delay if scroll_delay is not None else float(os.getenv("SCROLL_DELAY_SECONDS", "3.0"))

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(session_path),
            channel="chromium",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled", "--use-mock-keychain"],
        )
        _load_saved_cookies(context, session_path)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            return _scrape_single_page(
                page=page,
                group_url=group_url,
                group_name=group_name or "Standard-Gruppe",
                db=db,
                scroll_count=scroll_count,
                scroll_delay=scroll_delay,
                debug_graphql=debug_graphql,
            )
        finally:
            context.close()


def scrape_all_groups(
    session_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    headless: Optional[bool] = None,
    scroll_count: Optional[int] = None,
    scroll_delay: Optional[float] = None,
    debug_graphql: bool = False,
) -> Dict[str, int]:
    """
    Scrapt alle aktivierten Facebook-Gruppen aus state.db in einer gemeinsamen Browser-Session.
    Falls noch keine Gruppen in state.db gepflegt sind, wird FB_GROUP_URL aus .env automatisch angelegt.
    """
    db = StateDB(db_path or os.getenv("DB_PATH", "./state.db"))
    default_url = os.getenv("FB_GROUP_URL", "").strip()
    if default_url:
        db.ensure_default_group(default_url)

    groups = db.get_groups(only_enabled=True)
    if not groups:
        raise ValueError(
            "Keine aktiven Facebook-Gruppen konfiguriert. Bitte im Dashboard unter 'Gruppen & Login' eine Gruppe anlegen."
        )

    session_path = Path(session_dir or os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
    if not session_path.exists():
        raise FileNotFoundError(
            f"Session-Ordner {session_path} existiert nicht. Bitte zuerst im Dashboard auf 'Login aktualisieren' klicken."
        )

    if headless is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
    scroll_count = scroll_count if scroll_count is not None else int(os.getenv("SCROLL_COUNT", "4"))
    scroll_delay = scroll_delay if scroll_delay is not None else float(os.getenv("SCROLL_DELAY_SECONDS", "3.0"))
    fetch_comments = os.getenv("FETCH_POST_COMMENTS", "true").lower() == "true"
    max_comment_posts = int(os.getenv("MAX_COMMENT_POSTS_PER_RUN", "8"))

    totals = {
        "groups_scraped": 0,
        "graphql_responses": 0,
        "posts_extracted": 0,
        "new_or_updated": 0,
    }

    print(f"[Scraper] Starte Browser (headless={headless}) für {len(groups)} aktive Gruppe(n) ...")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(session_path),
            channel="chromium",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled", "--use-mock-keychain"],
        )
        _load_saved_cookies(context, session_path)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for idx, grp in enumerate(groups, start=1):
                stats = _scrape_single_page(
                    page=page,
                    group_url=grp["url"],
                    group_name=grp["name"],
                    db=db,
                    scroll_count=scroll_count,
                    scroll_delay=scroll_delay,
                    debug_graphql=debug_graphql,
                    debug_prefix=f"group{grp['id']}_graphql",
                    fetch_comments=fetch_comments,
                    max_comment_posts=max_comment_posts,
                )
                totals["groups_scraped"] += 1
                totals["graphql_responses"] += stats["graphql_responses"]
                totals["posts_extracted"] += stats["posts_extracted"]
                totals["new_or_updated"] += stats["new_or_updated"]

                # Kurze menschliche Pause zwischen mehreren Gruppen
                if idx < len(groups):
                    pause = random.uniform(3.0, 6.0)
                    print(f"[Scraper] Warte {pause:.1f}s vor der nächsten Gruppe ...")
                    time.sleep(pause)
        finally:
            context.close()

    print(
        f"[Scraper] Gesamt: {totals['groups_scraped']} Gruppe(n), "
        f"{totals['graphql_responses']} GraphQL-Antworten, "
        f"{totals['posts_extracted']} Posts extrahiert, {totals['new_or_updated']} neu/aktualisiert."
    )
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook-Gruppen-Scraper via GraphQL-Interception")
    parser.add_argument("--debug-graphql", action="store_true", help="Speichert rohe GraphQL-Responses in ./debug_graphql/")
    parser.add_argument("--visible", action="store_true", help="Startet den Browser sichtbar (headless=False)")
    args = parser.parse_args()

    scrape_all_groups(
        headless=False if args.visible else None,
        debug_graphql=args.debug_graphql,
    )
