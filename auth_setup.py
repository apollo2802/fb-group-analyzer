from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()


def run_auth_setup(gui_mode: bool = False) -> None:
    session_dir = Path(os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
    group_url = os.getenv("FB_GROUP_URL", "").strip()
    target_url = group_url if group_url and "DEINE_GRUPPE" not in group_url else "https://www.facebook.com/"

    session_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Facebook Session-Setup")
    print("=" * 60)
    print(f"Profil-Verzeichnis : {session_dir}")
    print(f"Start-URL          : {target_url}")
    print()
    print("Anleitung:")
    print("1. Logge dich im geöffneten Browser-Fenster manuell bei Facebook ein.")
    print("2. Bestätige ggf. Cookie-Banner und 2FA-Abfragen.")
    print("3. Schließe danach einfach das Browser-Fenster – die Session wird gespeichert.")
    print("=" * 60)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)

        try:
            if gui_mode:
                # Im Dashboard-Modus gibt es kein interaktives Terminal:
                # warten, bis der Benutzer das Browser-Fenster schließt.
                context.wait_for_event("close", timeout=0)
            else:
                input("\nDrücke Enter (oder schließe das Browser-Fenster), um die Session zu speichern... ")
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    print(f"\nSession erfolgreich gespeichert in: {session_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Einmaliger Facebook-Login mit persistentem Profil")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Wartet auf das Schließen des Browser-Fensters (für Start aus dem Dashboard)",
    )
    args = parser.parse_args()
    run_auth_setup(gui_mode=args.gui)
