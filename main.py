from __future__ import annotations

import argparse
import os
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from analyzer import analyze_pending_posts
from db import StateDB
from dispatcher import dispatch_activities
from scraper import scrape_group

load_dotenv()


def run_pipeline(debug_graphql: bool = False, visible: bool = False) -> None:
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starte Pipeline-Durchlauf ...")
    try:
        scrape_group(
            headless=False if visible else None,
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
    parser.add_argument("--stats", action="store_true", help="Zeigt aktuelle Datenbank-Statistiken an")
    args = parser.parse_args()

    if args.stats:
        db = StateDB(os.getenv("DB_PATH", "./state.db"))
        print(db.get_stats())
        return

    if args.scrape_only:
        scrape_group(headless=False if args.visible else None, debug_graphql=args.debug_graphql)
        return

    if args.analyze_only:
        analyze_pending_posts()
        return

    if args.dispatch_only:
        dispatch_activities()
        return

    if args.once:
        run_pipeline(debug_graphql=args.debug_graphql, visible=args.visible)
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

    scheduler = BlockingScheduler()

    def run_and_schedule_next() -> None:
        run_pipeline(debug_graphql=args.debug_graphql, visible=args.visible)
        delay_seconds = random.randint(min_minutes * 60, max_minutes * 60)
        next_run = datetime.now() + timedelta(seconds=delay_seconds)
        mins, secs = divmod(delay_seconds, 60)
        print(
            f"[Main] Nächster Prüflauf geplant für {next_run.strftime('%H:%M:%S')} "
            f"(in {mins} Min. {secs} Sek.)."
        )
        scheduler.add_job(
            run_and_schedule_next,
            "date",
            run_date=next_run,
            max_instances=1,
            coalesce=True,
        )

    # Sofortigen ersten Durchlauf starten und danach zufällig weiterplanen
    run_and_schedule_next()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("\n[Main] Scheduler beendet.")


if __name__ == "__main__":
    main()
