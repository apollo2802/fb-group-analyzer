from __future__ import annotations

import argparse
import atexit
import os
import random
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from analyzer import analyze_pending_posts
from db import StateDB
from dispatcher import dispatch_activities
from scraper import scrape_all_groups

load_dotenv()


def run_pipeline(
    debug_graphql: bool = False,
    visible: bool = False,
    scroll_count: int | None = None,
) -> None:
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starte Pipeline-Durchlauf ...")
    try:
        scrape_all_groups(
            headless=False if visible else None,
            scroll_count=scroll_count,
            debug_graphql=debug_graphql,
        )
        analyze_pending_posts()
        dispatch_activities()
    except Exception as exc:
        print(f"[Pipeline] Fehler im Durchlauf: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Group Analyzer Orchestrator")
    parser.add_argument("--once", action="store_true", help="Führt die gesamte Pipeline einmalig aus")
    parser.add_argument("--scrape-only", action="store_true", help="Führt nur den Scraper aus")
    parser.add_argument("--analyze-only", action="store_true", help="Führt nur die KI-Analyse aus")
    parser.add_argument("--dispatch-only", action="store_true", help="Führt nur den Dispatcher aus")
    parser.add_argument("--debug-graphql", action="store_true", help="Speichert rohe GraphQL-Responses zum Debuggen")
    parser.add_argument("--visible", action="store_true", help="Startet den Browser sichtbar (headless=False)")
    parser.add_argument("--scroll-count", type=int, default=None, help="Anzahl der Scroll-Schritte pro Gruppe")
    parser.add_argument("--stats", action="store_true", help="Zeigt aktuelle Datenbank-Statistiken an")
    args = parser.parse_args()

    if args.stats:
        db = StateDB(os.getenv("DB_PATH", "./state.db"))
        print(db.get_stats())
        return

    if args.scrape_only:
        scrape_all_groups(
            headless=False if args.visible else None,
            scroll_count=args.scroll_count,
            debug_graphql=args.debug_graphql,
        )
        return

    if args.analyze_only:
        analyze_pending_posts()
        return

    if args.dispatch_only:
        dispatch_activities()
        return

    if args.once:
        run_pipeline(
            debug_graphql=args.debug_graphql,
            visible=args.visible,
            scroll_count=args.scroll_count,
        )
        return

    fallback_interval = int(os.getenv("CHECK_INTERVAL_MINUTES", "20"))
    min_minutes = int(os.getenv("CHECK_INTERVAL_MIN_MINUTES", str(max(5, fallback_interval - 5))))
    max_minutes = int(os.getenv("CHECK_INTERVAL_MAX_MINUTES", str(fallback_interval + 10)))
    if max_minutes < min_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes

    print(
        f"[Main] Starte Scheduler mit zufälligem Intervall "
        f"({min_minutes}–{max_minutes} Minuten). Beenden mit Strg+C."
    )

    db = StateDB(os.getenv("DB_PATH", "./state.db"))
    pid_str = str(os.getpid())

    def mark_stopped() -> None:
        try:
            current = db.get_runtime_state()
            if current.get("scheduler_pid") == pid_str:
                db.set_runtime_state(
                    {
                        "scheduler_state": "stopped",
                        "scheduler_pid": "",
                        "next_run_at": "",
                    }
                )
        except Exception:
            pass

    atexit.register(mark_stopped)

    scheduler = BlockingScheduler()

    def write_heartbeat() -> None:
        try:
            db.set_runtime_state(
                {
                    "scheduler_pid": pid_str,
                    "scheduler_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            pass

    def run_and_schedule_next() -> None:
        db.set_runtime_state(
            {
                "scheduler_pid": pid_str,
                "scheduler_state": "scraping",
                "last_run_started_at": datetime.now(timezone.utc).isoformat(),
                "scheduler_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        run_pipeline(
            debug_graphql=args.debug_graphql,
            visible=args.visible,
            scroll_count=args.scroll_count,
        )
        delay_seconds = random.randint(min_minutes * 60, max_minutes * 60)
        next_run = datetime.now() + timedelta(seconds=delay_seconds)
        mins, secs = divmod(delay_seconds, 60)
        print(
            f"[Main] Nächster Prüflauf geplant für {next_run.strftime('%H:%M:%S')} "
            f"(in {mins} Min. {secs} Sek.)."
        )
        db.set_runtime_state(
            {
                "scheduler_pid": pid_str,
                "scheduler_state": "idle",
                "last_run_finished_at": datetime.now(timezone.utc).isoformat(),
                "next_run_at": next_run.astimezone().isoformat(),
                "scheduler_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        scheduler.add_job(
            run_and_schedule_next,
            "date",
            run_date=next_run,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
        )

    scheduler.add_job(
        write_heartbeat,
        "interval",
        seconds=60,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )

    # Sofortigen ersten Durchlauf starten und danach zufällig weiterplanen
    run_and_schedule_next()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        mark_stopped()
        print("\n[Main] Scheduler beendet.")


if __name__ == "__main__":
    main()
