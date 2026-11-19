from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List

import streamlit as st
from dotenv import load_dotenv

from db import StateDB

load_dotenv()

st.set_page_config(
    page_title="Facebook Group Analyzer",
    page_icon="📊",
    layout="wide",
)

DB_PATH = os.getenv("DB_PATH", "./state.db")
db = StateDB(DB_PATH)

PRIORITY_BADGE = {
    "High": "🔴 Hoch",
    "Medium": "🟠 Mittel",
    "Low": "🟢 Niedrig",
}

SENTIMENT_BADGE = {
    "Positiv": "😊 Positiv",
    "Neutral": "😐 Neutral",
    "Kritisch/Negativ": "😟 Kritisch",
}

STATUS_LABELS = {
    "open": "Offen",
    "done": "Erledigt",
    "ignored": "Ignoriert",
}


def load_enriched_posts() -> List[Dict[str, Any]]:
    raw_posts = db.get_all_posts()
    enriched: List[Dict[str, Any]] = []
    for p in raw_posts:
        action: Dict[str, Any] = {}
        if p.get("extracted_actions"):
            try:
                action = json.loads(p["extracted_actions"])
            except json.JSONDecodeError:
                action = {}

        comments: List[Dict[str, Any]] = []
        if p.get("comments"):
            try:
                comments = json.loads(p["comments"])
            except json.JSONDecodeError:
                comments = []

        raw_score = action.get("sentiment_score", 0)
        try:
            sentiment_score = max(-100, min(100, int(raw_score)))
        except (TypeError, ValueError):
            sentiment_score = 0

        sentiment = action.get("sentiment") or "Neutral"
        if sentiment not in SENTIMENT_BADGE:
            sentiment = "Neutral"

        enriched.append(
            {
                "post_id": p["post_id"],
                "author": p.get("author") or "Unbekannt",
                "content": p.get("content") or "",
                "comments": comments,
                "comment_count": p.get("comment_count") or 0,
                "timestamp": p.get("timestamp") or p.get("created_at") or "",
                "post_url": p.get("post_url") or "",
                "status": p.get("status") or "open",
                "needs_analysis": bool(p.get("needs_analysis")),
                "is_actionable": bool(action.get("is_actionable", False)),
                "category": action.get("category", "Info"),
                "action_title": action.get("action_title", "(Noch nicht analysiert)"),
                "details": action.get("details", ""),
                "due_date": action.get("due_date") or "",
                "priority": action.get("priority", "Low"),
                "sentiment": sentiment,
                "sentiment_score": sentiment_score,
            }
        )
    return enriched


def to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    fieldnames = [
        "timestamp",
        "post_id",
        "status",
        "is_actionable",
        "category",
        "priority",
        "sentiment",
        "sentiment_score",
        "due_date",
        "action_title",
        "details",
        "author",
        "post_url",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue().encode("utf-8")


def format_timestamp(ts: str) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return ts


def render_sentiment_barometer(posts: List[Dict[str, Any]]) -> None:
    """Rendert das visuelle Stimmungsbarometer für die gesamte Gruppe."""
    if not posts:
        return

    avg_score = round(sum(p["sentiment_score"] for p in posts) / len(posts))
    pos_count = sum(1 for p in posts if p["sentiment"] == "Positiv")
    neu_count = sum(1 for p in posts if p["sentiment"] == "Neutral")
    neg_count = sum(1 for p in posts if p["sentiment"] == "Kritisch/Negativ")

    # Position auf Skala 0% (-100) bis 100% (+100)
    marker_pct = max(2, min(98, (avg_score + 100) / 2))

    if avg_score >= 20:
        mood_label = "😊 Überwiegend positiv"
        mood_color = "#2b8a3e"
    elif avg_score <= -20:
        mood_label = "😟 Kritisch / angespannt"
        mood_color = "#c92a2a"
    else:
        mood_label = "😐 Ausgeglichen / neutral"
        mood_color = "#e67700"

    with st.container(border=True):
        st.subheader("🌡️ Stimmungsbarometer der Gruppe")
        left, right = st.columns([3, 2])

        with left:
            bar_html = f"""
            <div style="padding: 8px 0 4px 0;">
                <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px;">
                    <span style="font-size:1.15rem; font-weight:700; color:{mood_color};">{mood_label}</span>
                    <span style="font-size:1.1rem; font-weight:700;">Index: {avg_score:+d} / 100</span>
                </div>
                <div style="position:relative; height:22px; border-radius:999px;
                            background: linear-gradient(90deg, #e03131 0%, #f08c00 35%, #ffd43b 50%, #74b816 75%, #2b8a3e 100%);
                            box-shadow: inset 0 1px 3px rgba(0,0,0,0.2);">
                    <div style="position:absolute; left:{marker_pct:.1f}%; top:-5px; transform:translateX(-50%);
                                width:14px; height:32px; border-radius:6px; background:#1e1e1e;
                                border:2px solid #ffffff; box-shadow:0 2px 6px rgba(0,0,0,0.35);">
                    </div>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.8rem; color:#6c757d; margin-top:6px;">
                    <span>-100 (Kritisch)</span>
                    <span>0 (Neutral)</span>
                    <span>+100 (Positiv)</span>
                </div>
            </div>
            """
            st.markdown(bar_html, unsafe_allow_html=True)

        with right:
            c1, c2, c3 = st.columns(3)
            c1.metric("😊 Positiv", pos_count)
            c2.metric("😐 Neutral", neu_count)
            c3.metric("😟 Kritisch", neg_count)


# --- Sidebar ---
st.sidebar.title("⚙️ Steuerung & Filter")

if st.sidebar.button("🔄 Ansicht aktualisieren", use_container_width=True):
    st.rerun()

if st.sidebar.button("🚀 Jetzt scrapen & analysieren", type="primary", use_container_width=True):
    with st.sidebar.status("Pipeline läuft ...", expanded=True) as status_box:
        try:
            st.write("Starte Scraper, KI-Analyse & Export ...")
            result = subprocess.run(
                [sys.executable, "main.py", "--once"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            output_lines = [
                line
                for line in (result.stdout or "").splitlines()
                if line.startswith("[")
            ]
            for line in output_lines:
                st.write(line)

            if result.returncode != 0 or any("Fehler im Durchlauf" in l for l in output_lines):
                status_box.update(label="Fehler aufgetreten", state="error")
                if result.stderr:
                    st.sidebar.error(result.stderr[-500:])
            else:
                status_box.update(label="Pipeline abgeschlossen!", state="complete")
                st.rerun()
        except Exception as exc:
            status_box.update(label="Fehler aufgetreten", state="error")
            st.sidebar.error(str(exc))

st.sidebar.divider()

only_actionable = st.sidebar.toggle("Nur relevante Aktivitäten (Actionable)", value=False)

all_categories = ["Termin/Event", "Frage/Support", "Bedarf/Anfrage", "To-Do", "Info"]
selected_categories = st.sidebar.multiselect(
    "Kategorien",
    options=all_categories,
    default=all_categories,
)

all_priorities = ["High", "Medium", "Low"]
selected_priorities = st.sidebar.multiselect(
    "Prioritäten",
    options=all_priorities,
    default=all_priorities,
)

all_sentiments = ["Positiv", "Neutral", "Kritisch/Negativ"]
selected_sentiments = st.sidebar.multiselect(
    "Stimmung",
    options=all_sentiments,
    default=all_sentiments,
    format_func=lambda s: SENTIMENT_BADGE.get(s, s),
)

selected_statuses = st.sidebar.multiselect(
    "Bearbeitungsstatus",
    options=["open", "done", "ignored"],
    default=["open", "done"],
    format_func=lambda s: STATUS_LABELS.get(s, s),
)

search_query = st.sidebar.text_input("🔍 Suche (Titel, Details, Text)", placeholder="Stichwort...")

# --- Daten laden ---
posts = load_enriched_posts()

# --- Header & KPIs ---
st.title("📊 Facebook Group Analyzer")
st.caption(f"Gruppe: {os.getenv('FB_GROUP_URL', 'Nicht konfiguriert')}  |  Datenbank: `{DB_PATH}`")

total_count = len(posts)
open_actionable = sum(1 for p in posts if p["is_actionable"] and p["status"] == "open")
high_prio_open = sum(
    1 for p in posts if p["is_actionable"] and p["status"] == "open" and p["priority"] == "High"
)
done_count = sum(1 for p in posts if p["status"] == "done")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Erfasste Beiträge", total_count)
col2.metric("Offene Aktivitäten", open_actionable)
col3.metric("Hohe Priorität (offen)", high_prio_open)
col4.metric("Erledigt", done_count)

# --- Stimmungsbarometer ---
render_sentiment_barometer(posts)

# --- Filtern ---
filtered: List[Dict[str, Any]] = []
q = search_query.strip().lower()
for p in posts:
    if only_actionable and not p["is_actionable"]:
        continue
    if p["category"] not in selected_categories:
        continue
    if p["priority"] not in selected_priorities:
        continue
    if p["sentiment"] not in selected_sentiments:
        continue
    if p["status"] not in selected_statuses:
        continue
    if q:
        haystack = " ".join(
            [
                p["action_title"],
                p["details"],
                p["content"],
                p["author"],
                " ".join(c.get("text", "") for c in p["comments"]),
            ]
        ).lower()
        if q not in haystack:
            continue
    filtered.append(p)

st.divider()

tab_cards, tab_table = st.tabs([f"📋 Aktivitäten ({len(filtered)})", "📑 Tabelle & Export"])

with tab_cards:
    if not filtered:
        st.info("Keine Beiträge entsprechen den aktuellen Filterkriterien.")
    else:
        for p in filtered:
            with st.container(border=True):
                top_left, top_right = st.columns([4, 1])
                with top_left:
                    action_icon = "⚡" if p["is_actionable"] else "ℹ️"
                    st.subheader(f"{action_icon} {p['action_title']}")
                    sent_label = f"{SENTIMENT_BADGE.get(p['sentiment'], p['sentiment'])} ({p['sentiment_score']:+d})"
                    meta_parts = [
                        f"**Kategorie:** {p['category']}",
                        f"**Priorität:** {PRIORITY_BADGE.get(p['priority'], p['priority'])}",
                        f"**Stimmung:** {sent_label}",
                        f"**Status:** {STATUS_LABELS.get(p['status'], p['status'])}",
                        f"**Datum:** {format_timestamp(p['timestamp'])}",
                    ]
                    if p["due_date"]:
                        meta_parts.append(f"**Fällig:** 📅 `{p['due_date']}`")
                    st.markdown("  |  ".join(meta_parts))

                with top_right:
                    if p["status"] != "done":
                        if st.button("✅ Erledigt", key=f"done_{p['post_id']}", use_container_width=True):
                            db.update_status(p["post_id"], "done")
                            st.rerun()
                    else:
                        if st.button("↩️ Öffnen", key=f"open_{p['post_id']}", use_container_width=True):
                            db.update_status(p["post_id"], "open")
                            st.rerun()

                    if p["status"] != "ignored":
                        if st.button("🚫 Ignorieren", key=f"ign_{p['post_id']}", use_container_width=True):
                            db.update_status(p["post_id"], "ignored")
                            st.rerun()

                if p["details"]:
                    st.write(p["details"])

                with st.expander(f"Original-Beitrag & Kommentare ({p['comment_count']})"):
                    st.markdown(f"**Autor:** {p['author']}")
                    st.write(p["content"] or "*(kein Text)*")
                    if p["comments"]:
                        st.markdown("**Kommentare:**")
                        for c in p["comments"]:
                            st.markdown(f"- **{c.get('author', 'Unbekannt')}:** {c.get('text', '')}")
                    if p["post_url"]:
                        st.markdown(f"[🔗 Beitrag auf Facebook öffnen]({p['post_url']})")

with tab_table:
    if not filtered:
        st.info("Keine Daten zum Anzeigen.")
    else:
        table_rows = [
            {
                "Zeitpunkt": format_timestamp(p["timestamp"]),
                "Relevant": "Ja" if p["is_actionable"] else "Nein",
                "Status": STATUS_LABELS.get(p["status"], p["status"]),
                "Kategorie": p["category"],
                "Priorität": p["priority"],
                "Stimmung": f"{SENTIMENT_BADGE.get(p['sentiment'], p['sentiment'])} ({p['sentiment_score']:+d})",
                "Fällig": p["due_date"] or "-",
                "Titel": p["action_title"],
                "Details": p["details"],
                "Kommentare": p["comment_count"],
                "Link": p["post_url"],
            }
            for p in filtered
        ]
        st.dataframe(
            table_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Link": st.column_config.LinkColumn("Facebook-Link", display_text="Öffnen"),
            },
        )
        st.download_button(
            label="⬇️ Gefilterte Liste als CSV herunterladen",
            data=to_csv_bytes(filtered),
            file_name="facebook_activities.csv",
            mime="text/csv",
        )
