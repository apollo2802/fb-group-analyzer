from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from db import StateDB

load_dotenv()


def run_auth_setup(gui_mode: bool = False) -> None:
    session_dir = Path(os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
    state_file = session_dir.parent / "storage_state.json"
    group_url = os.getenv("FB_GROUP_URL", "").strip()
    target_url = group_url if group_url and "DEINE_GRUPPE" not in group_url else "https://www.facebook.com/"

    session_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Facebook Session-Setup")
    print("=" * 60)
    print(f"Profil-Verzeichnis : {session_dir}")
    print(f"Cookie-Backup      : {state_file}")
    print(f"Start-URL          : {target_url}")
    print()
    print("Anleitung:")
    print("1. Logge dich im geöffneten Browser-Fenster manuell bei Facebook ein.")
    print("2. Bestätige ggf. Cookie-Banner und 2FA-Abfragen.")
    print("3. Schließe danach einfach das Browser-Fenster – die Session wird gespeichert.")
    print("=" * 60)

    def save_login_state(ctx, db_obj: StateDB) -> bool:
        cookies = ctx.cookies("https://www.facebook.com")
        if any(c.get("name") == "c_user" and c.get("value") for c in cookies):
            ctx.storage_state(path=str(state_file))
            try:
                os.chmod(state_file, 0o600)
            except OSError:
                pass
            db_obj.set_runtime_state(
                {
                    "fb_session_status": "ok",
                    "fb_session_message": f"Erneuert am {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                }
            )
            return True
        return False

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(session_dir),
            channel="chromium",
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            args=["--disable-blink-features=AutomationControlled", "--use-mock-keychain"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)

        db = StateDB(os.getenv("DB_PATH", "./state.db"))
        closed = False

        def on_close(_ctx=None) -> None:
            nonlocal closed
            closed = True

        context.on("close", on_close)

        try:
            if gui_mode:
                # Solange das Browser-Fenster offen ist, regelmäßig auf das Login-Cookie c_user prüfen
                # und sowohl storage_state.json als auch state.db sofort aktualisieren.
                marked_ok = False
                while not closed and context.pages:
                    try:
                        if save_login_state(context, db) and not marked_ok:
                            print(f"[Auth] Login erkannt & gespeichert in {state_file}")
                            marked_ok = True
                        page.wait_for_timeout(1500)
                    except Exception:
                        break
            else:
                input("\nDrücke Enter (oder schließe das Browser-Fenster), um die Session zu speichern... ")
                try:
                    save_login_state(context, db)
                except Exception:
                    pass
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
