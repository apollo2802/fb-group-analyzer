from __future__ import annotations

import csv
import html
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
from dotenv import load_dotenv

from db import StateDB

load_dotenv()

st.set_page_config(
    page_title="Facebook Group Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = os.getenv("DB_PATH", "./state.db")
SESSION_DIR = Path(os.getenv("FB_SESSION_DIR", "./fb_session")).resolve()
db = StateDB(DB_PATH)

_default_env_url = os.getenv("FB_GROUP_URL", "").strip()
if _default_env_url:
    db.ensure_default_group(_default_env_url)

STATUS_LABELS = {
    "open": "Offen",
    "done": "Erledigt",
    "ignored": "Ignoriert",
}

SENTIMENT_LABELS = {
    "Positiv": "😊 Positiv",
    "Neutral": "😐 Neutral",
    "Kritisch/Negativ": "😟 Kritisch",
}

PRIORITY_META = {
    "High": ("Hoch", "pill-high", "prio-high"),
    "Medium": ("Mittel", "pill-med", "prio-medium"),
    "Low": ("Niedrig", "pill-low", "prio-low"),
}


def inject_design_system() -> None:
    st.html(
        """
        <style>
        /* Clean SaaS chrome & spacing */
        [data-testid="stDecoration"], #MainMenu, footer { display: none !important; }
        header[data-testid="stHeader"] { background: transparent !important; height: 2.2rem; }
        .block-container {
            padding: 1.4rem 2.2rem 3rem !important;
            max-width: 1380px !important;
        }
        [data-testid="stSidebar"] {
            background: #FFFFFF !important;
            border-right: 1px solid #E2E8F0 !important;
        }
        [data-testid="stSidebar"] .block-container {
            padding-top: 1.5rem !important;
        }

        /* Card elevation & dynamic accents (compatible with Streamlit 1.67 DOM) */
        div[data-testid="stVerticalBlockBorderWrapper"],
        div[data-testid="stVerticalBlock"][style*="border"] {
            background: #FFFFFF;
            border-radius: 14px !important;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.05);
            transition: border-color 150ms ease, box-shadow 150ms ease;
        }
        .card-accent {
            border-left: 4px solid #CBD5E1;
            padding-left: 12px;
            border-radius: 2px;
        }
        .card-accent.prio-high { border-left-color: #DC2626; }
        .card-accent.prio-medium { border-left-color: #D97706; }
        .card-accent.prio-low { border-left-color: #10B981; }
        .card-dimmed { opacity: 0.68; }
        .group-card-selected {
            border-left: 4px solid #4F46E5;
            padding-left: 10px;
            border-radius: 2px;
        }

        /* Pure-CSS Circular Sentiment Gauge (immune to SVG sanitization) */
        .gauge-ring {
            width: 74px;
            height: 74px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            position: relative;
        }
        .gauge-inner {
            width: 56px;
            height: 56px;
            border-radius: 50%;
            background: #FFFFFF;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.06);
        }
        .gauge-score {
            font-size: 1.02rem;
            font-weight: 800;
            color: #0F172A;
            line-height: 1;
        }
        .gauge-sub {
            font-size: 0.62rem;
            font-weight: 600;
            color: #94A3B8;
            margin-top: 2px;
        }

        /* Top header */
        .app-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 18px;
        }
        .app-title {
            font-size: 1.65rem;
            font-weight: 800;
            letter-spacing: -0.025em;
            color: #0F172A;
            margin: 0;
            line-height: 1.2;
        }
        .app-subtitle {
            font-size: 0.88rem;
            color: #64748B;
            margin: 2px 0 0 0;
        }

        /* KPI strip */
        .kpi-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 14px;
            margin-bottom: 22px;
        }
        @media (max-width: 820px) {
            .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        .kpi-card {
            background: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 14px;
            padding: 14px 18px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
        }
        .kpi-label {
            font-size: 0.78rem;
            font-weight: 600;
            color: #64748B;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 6px;
        }
        .kpi-value {
            font-size: 1.75rem;
            font-weight: 800;
            color: #0F172A;
            line-height: 1.1;
        }
        .kpi-accent-red { color: #DC2626; }
        .kpi-accent-indigo { color: #4F46E5; }
        .kpi-accent-green { color: #059669; }

        /* Section titles */
        .section-heading {
            font-size: 0.85rem;
            font-weight: 700;
            color: #475569;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin: 4px 0 10px 0;
        }

        /* Pill badges */
        .pill-row {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
            margin: 0 0 8px 0;
        }
        .pill {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 10px;
            border-radius: 999px;
            font-size: 0.74rem;
            font-weight: 600;
            line-height: 1.5;
            border: 1px solid transparent;
            white-space: nowrap;
        }
        .pill-group { background: #F1F5F9; color: #334155; border-color: #E2E8F0; }
        .pill-cat { background: #EEF2FF; color: #4338CA; border-color: #C7D2FE; }
        .pill-high, .pill-neg { background: #FEF2F2; color: #B91C1C; border-color: #FECACA; }
        .pill-med, .pill-neu { background: #FFFBEB; color: #B45309; border-color: #FDE68A; }
        .pill-low, .pill-pos { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
        .pill-due {
            background: #F8FAFC;
            color: #475569;
            border-color: #CBD5E1;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        }
        .pill-status-done { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
        .pill-status-ignored { background: #F1F5F9; color: #64748B; border-color: #E2E8F0; }
        .pill-active { background: #ECFDF5; color: #047857; border-color: #A7F3D0; }
        .pill-paused { background: #F1F5F9; color: #64748B; border-color: #E2E8F0; }

        /* Activity card typography */
        .card-top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 6px;
        }
        .card-timestamp {
            font-size: 0.78rem;
            color: #64748B;
            white-space: nowrap;
        }
        .card-title {
            font-size: 1.08rem;
            font-weight: 700;
            color: #0F172A;
            margin: 2px 0 6px 0;
            line-height: 1.35;
        }
        .card-details {
            font-size: 0.91rem;
            color: #334155;
            line-height: 1.55;
            margin: 0 0 6px 0;
        }

        /* Comment bubble in expander */
        .comment-item {
            background: #F8FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 10px;
            padding: 8px 12px;
            margin-bottom: 6px;
            font-size: 0.87rem;
            color: #1E293B;
        }
        .comment-author {
            font-weight: 700;
            color: #0F172A;
            margin-right: 6px;
        }

        /* Sidebar status pill */
        .sidebar-brand {
            font-size: 1.05rem;
            font-weight: 800;
            color: #0F172A;
            letter-spacing: -0.02em;
            margin-bottom: 2px;
        }
        .sidebar-sub {
            font-size: 0.78rem;
            color: #64748B;
            margin-bottom: 14px;
        }
        </style>
        """
    )


def _esc(val: Any) -> str:
    return html.escape(str(val or ""), quote=True)


def _safe_url(url: str) -> str:
    cleaned = (url or "").strip()
    return cleaned if cleaned.startswith(("https://", "http://")) else ""


def format_timestamp(ts: str) -> str:
    if not ts:
        return "Noch nicht geprüft"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y · %H:%M")
    except ValueError:
        return ts


@st.cache_data(ttl=15)
def load_enriched_posts(db_path: str) -> List[Dict[str, Any]]:
    local_db = StateDB(db_path)
    raw_posts = local_db.get_all_posts()
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
        if sentiment not in SENTIMENT_LABELS:
            sentiment = "Neutral"

        enriched.append(
            {
                "post_id": p["post_id"],
                "group_name": p.get("group_name") or "Standard-Gruppe",
                "group_url": p.get("group_url") or "",
                "author": p.get("author") or "Unbekannt",
                "content": p.get("content") or "",
                "comments": comments,
                "comment_count": p.get("comment_count") or 0,
                "timestamp": p.get("timestamp") or p.get("created_at") or "",
                "post_url": _safe_url(p.get("post_url") or ""),
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
        "group_name",
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


def launch_login_browser() -> None:
    subprocess.Popen([sys.executable, "auth_setup.py", "--gui"])


def is_pid_running(pid_str: str) -> bool:
    if not pid_str:
        return False
    try:
        pid = int(pid_str)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def format_time_only(ts: str) -> str:
    if not ts:
        return "–"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ts


def start_scheduler_process() -> None:
    subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_scheduler_process(pid_str: str) -> None:
    try:
        os.kill(int(pid_str), signal.SIGTERM)
    except Exception:
        pass
    db.set_runtime_state(
        {"scheduler_state": "stopped", "scheduler_pid": "", "next_run_at": ""}
    )


def render_group_barometer_card(
    grp: Dict[str, Any],
    grp_posts: List[Dict[str, Any]],
    is_selected: bool,
) -> None:
    count = len(grp_posts)
    if count > 0:
        avg_score = round(sum(p["sentiment_score"] for p in grp_posts) / count)
        pos_count = sum(1 for p in grp_posts if p["sentiment"] == "Positiv")
        neu_count = sum(1 for p in grp_posts if p["sentiment"] == "Neutral")
        neg_count = sum(1 for p in grp_posts if p["sentiment"] == "Kritisch/Negativ")
        pos_pct = (pos_count / count) * 100
        neu_pct = (neu_count / count) * 100
        neg_pct = (neg_count / count) * 100
    else:
        avg_score = 0
        pos_count = neu_count = neg_count = 0
        pos_pct = neu_pct = neg_pct = 0.0

    open_actionable = sum(1 for p in grp_posts if p["is_actionable"] and p["status"] == "open")
    high_open = sum(
        1 for p in grp_posts if p["is_actionable"] and p["status"] == "open" and p["priority"] == "High"
    )

    if count == 0:
        mood_label = "Keine Daten"
        mood_color = "#CBD5E1"
        mood_pill_cls = "pill-group"
        fill_deg = 0.0
        score_text = "–"
    else:
        fill_deg = max(8.0, min(360.0, ((avg_score + 100) / 200.0) * 360.0))
        score_text = f"{avg_score:+d}"
        if avg_score >= 20:
            mood_label = "Positiv"
            mood_color = "#10B981"
            mood_pill_cls = "pill-pos"
        elif avg_score <= -20:
            mood_label = "Kritisch"
            mood_color = "#EF4444"
            mood_pill_cls = "pill-neg"
        else:
            mood_label = "Ausgeglichen"
            mood_color = "#F59E0B"
            mood_pill_cls = "pill-neu"

    status_pill = (
        '<span class="pill pill-active">● Aktiv</span>'
        if grp.get("enabled", True)
        else '<span class="pill pill-paused">⏸ Pausiert</span>'
    )
    selected_hook = "group-card-selected" if is_selected else ""
    high_dot = (
        f'<span class="pill pill-high">● {high_open} Hoch</span>'
        if high_open > 0
        else ""
    )
    action_badge = (
        f'<span class="pill pill-cat">⚡ {open_actionable} offen</span>'
        if open_actionable > 0
        else '<span class="pill pill-group">✓ Keine offenen Tasks</span>'
    )

    card_html = f"""
    <div class="{selected_hook}" style="padding: 2px 2px 4px 2px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:12px;">
            <div style="min-width:0;">
                <div style="font-weight:700; font-size:1.05rem; color:#0F172A; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    {_esc(grp['name'])}
                </div>
                <div style="font-size:0.75rem; color:#64748B; margin-top:2px;">
                    {count} Beiträge · {_esc(format_timestamp(grp.get('last_scraped_at') or ''))}
                </div>
            </div>
            {status_pill}
        </div>

        <div style="display:flex; align-items:center; gap:16px; margin-bottom:12px;">
            <div class="gauge-ring" style="background: conic-gradient({mood_color} 0deg {fill_deg:.1f}deg, #E2E8F0 {fill_deg:.1f}deg 360deg);">
                <div class="gauge-inner">
                    <span class="gauge-score">{score_text}</span>
                    <span class="gauge-sub">Index</span>
                </div>
            </div>
            <div style="flex:1; min-width:0;">
                <div style="margin-bottom:8px;">
                    <span class="pill {mood_pill_cls}">{mood_label}</span>
                </div>
                <div style="display:flex; height:8px; border-radius:999px; overflow:hidden; background:#F1F5F9; gap:1px;">
                    <div style="width:{pos_pct:.1f}%; background:#10B981;"></div>
                    <div style="width:{neu_pct:.1f}%; background:#F59E0B;"></div>
                    <div style="width:{neg_pct:.1f}%; background:#EF4444;"></div>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.74rem; color:#64748B; margin-top:6px; font-weight:600;">
                    <span style="color:#047857;">😊 {pos_count} Pos</span>
                    <span style="color:#B45309;">😐 {neu_count} Neu</span>
                    <span style="color:#B91C1C;">😟 {neg_count} Krit</span>
                </div>
            </div>
        </div>

        <div style="display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-bottom:6px;">
            {action_badge}
            {high_dot}
        </div>
    </div>
    """
    st.html(card_html)


def render_activity_card(p: Dict[str, Any]) -> None:
    prio_label, prio_pill_cls, prio_hook = PRIORITY_META.get(
        p["priority"], ("Niedrig", "pill-low", "prio-low")
    )
    sent_cls = (
        "pill-pos"
        if p["sentiment"] == "Positiv"
        else ("pill-neg" if p["sentiment"] == "Kritisch/Negativ" else "pill-neu")
    )
    sent_text = f"{SENTIMENT_LABELS.get(p['sentiment'], p['sentiment'])} ({p['sentiment_score']:+d})"
    dim_cls = "card-dimmed" if p["status"] in ("done", "ignored") else ""

    due_html = (
        f'<span class="pill pill-due">📅 {_esc(p["due_date"])}</span>'
        if p["due_date"]
        else ""
    )
    status_html = ""
    if p["status"] == "done":
        status_html = '<span class="pill pill-status-done">✓ Erledigt</span>'
    elif p["status"] == "ignored":
        status_html = '<span class="pill pill-status-ignored">Ignoriert</span>'

    title_prefix = "⚡ " if p["is_actionable"] else ""

    with st.container(border=True):
        st.html(
            f"""
            <div class="card-accent {prio_hook} {dim_cls}">
                <div class="card-top-bar">
                    <div class="pill-row">
                        <span class="pill pill-group">👥 {_esc(p['group_name'])}</span>
                        <span class="pill pill-cat">{_esc(p['category'])}</span>
                        <span class="pill {prio_pill_cls}">● {prio_label}</span>
                        <span class="pill {sent_cls}">{_esc(sent_text)}</span>
                        {due_html}
                        {status_html}
                    </div>
                    <span class="card-timestamp">{_esc(format_timestamp(p['timestamp']))}</span>
                </div>
                <div class="card-title">{title_prefix}{_esc(p["action_title"])}</div>
                {f'<p class="card-details">{_esc(p["details"])}</p>' if p["details"] else ''}
            </div>
            """
        )

        act_spacer, act_btns = st.columns([3.0, 2.4], vertical_alignment="center")
        with act_btns:
            b1, b2 = st.columns(2)
            if p["status"] != "done":
                if b1.button("✅ Erledigt", key=f"done_{p['post_id']}", help="Als erledigt markieren", width="stretch"):
                    db.update_status(p["post_id"], "done")
                    st.cache_data.clear()
                    st.rerun()
            else:
                if b1.button("↩️ Öffnen", key=f"open_{p['post_id']}", help="Wieder öffnen", width="stretch"):
                    db.update_status(p["post_id"], "open")
                    st.cache_data.clear()
                    st.rerun()

            if p["status"] != "ignored":
                if b2.button("🚫 Ignorieren", key=f"ign_{p['post_id']}", help="Ignorieren", width="stretch"):
                    db.update_status(p["post_id"], "ignored")
                    st.cache_data.clear()
                    st.rerun()
            else:
                if b2.button("👁️ Zeigen", key=f"unign_{p['post_id']}", help="Wieder einblenden", width="stretch"):
                    db.update_status(p["post_id"], "open")
                    st.cache_data.clear()
                    st.rerun()

        with st.expander(f"Original-Beitrag & Kommentare ({p['comment_count']})"):
            st.markdown(f"**Autor:** {p['author']}")
            st.write(p["content"] or "*(kein Text)*")
            if p["comments"]:
                st.markdown("**Kommentare:**")
                for c in p["comments"]:
                    st.html(
                        f'<div class="comment-item">'
                        f'<span class="comment-author">{_esc(c.get("author", "Unbekannt"))}:</span>'
                        f'{_esc(c.get("text", ""))}'
                        f"</div>"
                    )
            if p["post_url"]:
                st.markdown(f"[🔗 Beitrag auf Facebook öffnen ↗]({p['post_url']})")


# --- Init UI & State ---
inject_design_system()
st.session_state.setdefault("active_group", None)

groups = db.get_groups(only_enabled=False)
active_groups_count = sum(1 for g in groups if g["enabled"])
posts = load_enriched_posts(DB_PATH)

# Falls Posts existieren, deren Gruppenname nicht mehr in `groups` steht, auch als virtuelle Gruppe anzeigen
known_group_names = {g["name"] for g in groups}
display_groups = list(groups)
for extra_name in sorted({p["group_name"] for p in posts} - known_group_names):
    display_groups.append(
        {
            "id": f"virtual_{extra_name}",
            "name": extra_name,
            "url": "",
            "enabled": 0,
            "last_scraped_at": "",
        }
    )

# --- Lean Operations Sidebar ---
with st.sidebar:
    st.html(
        """
        <div class="sidebar-brand">📊 FB Group Analyzer</div>
        <div class="sidebar-sub">Automatisierte Überwachung &amp; KI-Triage</div>
        """
    )

    default_scrolls = int(os.getenv("SCROLL_COUNT", "8"))
    scroll_depth = st.slider(
        "Scroll-Tiefe pro Gruppe",
        min_value=2,
        max_value=25,
        value=max(2, min(25, default_scrolls)),
        help="Mehr Scrolls erfassen bei großen Gruppen mehr ältere Beiträge (z. B. 15–20 beim ersten Einlesen).",
    )

    if st.button("🚀 Jetzt scrapen & analysieren", type="primary", width="stretch"):
        with st.status("Pipeline läuft ...", expanded=True) as status_box:
            try:
                st.write(
                    f"Starte Scraper ({active_groups_count} aktive Gruppe(n), {scroll_depth}× Scroll) ..."
                )
                result = subprocess.run(
                    [sys.executable, "main.py", "--once", "--scroll-count", str(scroll_depth)],
                    capture_output=True,
                    text=True,
                    timeout=max(300, scroll_depth * 25),
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
                        st.error(result.stderr[-500:])
                else:
                    status_box.update(label="Pipeline abgeschlossen!", state="complete")
                    st.cache_data.clear()
                    st.rerun()
            except Exception as exc:
                status_box.update(label="Fehler aufgetreten", state="error")
                st.error(str(exc))

    c_ref, c_log = st.columns(2)
    if c_ref.button("🔄 Aktualisieren", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    if c_log.button("🔑 Login", width="stretch"):
        try:
            launch_login_browser()
            st.success("Login-Browser geöffnet. Nach dem Einloggen Fenster schließen.")
        except Exception as exc:
            st.error(f"Fehler: {exc}")

    st.divider()

    auto_refresh_options = {
        "Aus": None,
        "Alle 30 Sek.": 30,
        "Alle 1 Min.": 60,
        "Alle 5 Min.": 300,
        "Alle 15 Min.": 900,
    }
    auto_refresh_label = st.selectbox(
        "⏱️ Auto-Aktualisierung",
        options=list(auto_refresh_options.keys()),
        index=2,  # Standard: Alle 1 Min.
        help="Prüft im gewählten Intervall, ob der Hintergrund-Scheduler neue Daten in state.db gespeichert hat, und aktualisiert die Ansicht automatisch.",
    )
    auto_refresh_sec = auto_refresh_options[auto_refresh_label]

    rt = db.get_runtime_state()
    session_ok = SESSION_DIR.exists() and any(SESSION_DIR.iterdir())
    fb_session_status = rt.get("fb_session_status", "ok" if session_ok else "expired")
    fb_session_msg = rt.get("fb_session_message", "")

    if not session_ok or fb_session_status == "expired":
        session_pill = '<span class="pill pill-neg">● Login abgelaufen!</span>'
    else:
        session_pill = '<span class="pill pill-active">● Session aktiv</span>'

    sched_pid = rt.get("scheduler_pid", "")
    sched_alive = is_pid_running(sched_pid)
    sched_state = rt.get("scheduler_state", "stopped") if sched_alive else "stopped"
    next_run_fmt = format_time_only(rt.get("next_run_at", "")) if sched_alive else "–"
    last_run_fmt = format_time_only(rt.get("last_run_finished_at", ""))

    if sched_alive and sched_state == "scraping":
        sched_pill = '<span class="pill pill-cat">● Prüflauf aktiv…</span>'
    elif sched_alive:
        sched_pill = '<span class="pill pill-active">● Online</span>'
    else:
        sched_pill = '<span class="pill pill-neg">● Gestoppt</span>'

    st.html(
        f"""
        <div style="background:#F8FAFC; border:1px solid #E2E8F0; border-radius:12px; padding:10px 12px; margin:10px 0;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                <span style="font-size:0.8rem; font-weight:700; color:#334155;">Scheduler (15–30 Min.)</span>
                {sched_pill}
            </div>
            <div style="font-size:0.75rem; color:#64748B; line-height:1.55;">
                Nächster Check: <strong>{_esc(next_run_fmt)}</strong><br/>
                Letzter Lauf: <strong>{_esc(last_run_fmt)}</strong>
            </div>
        </div>
        """
    )

    if sched_alive:
        if st.button("⏹️ Scheduler stoppen", width="stretch"):
            stop_scheduler_process(sched_pid)
            st.rerun()
    else:
        if st.button("▶️ Scheduler starten", type="primary", width="stretch"):
            start_scheduler_process()
            st.success("Scheduler wird gestartet…")
            st.rerun()

    st.html(
        f"""
        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:12px; margin-bottom:8px;">
            <span style="font-size:0.8rem; font-weight:600; color:#475569;">Facebook-Status</span>
            {session_pill}
        </div>
        <div style="font-size:0.76rem; color:#64748B;">
            Aktive Gruppen: <strong>{active_groups_count} / {len(groups)}</strong><br/>
            Stand: {datetime.now().strftime('%H:%M:%S')} Uhr
        </div>
        """
    )

    if auto_refresh_sec is not None:
        db_file = Path(DB_PATH)
        current_mtime = db_file.stat().st_mtime if db_file.exists() else 0.0
        st.session_state.setdefault("last_seen_db_mtime", current_mtime)

        @st.fragment(run_every=auto_refresh_sec)
        def _watch_db_changes() -> None:
            latest_mtime = db_file.stat().st_mtime if db_file.exists() else 0.0
            if latest_mtime != st.session_state.get("last_seen_db_mtime"):
                st.session_state["last_seen_db_mtime"] = latest_mtime
                st.cache_data.clear()
                st.rerun(scope="app")

        _watch_db_changes()

# --- Main Header ---
active_group: Optional[str] = st.session_state.get("active_group")
if active_group and active_group not in {g["name"] for g in display_groups}:
    st.session_state["active_group"] = None
    active_group = None

scope_posts = [p for p in posts if p["group_name"] == active_group] if active_group else posts

st.html(
    f"""
    <div class="app-header">
        <div>
            <h1 class="app-title">Facebook Group Analyzer</h1>
            <p class="app-subtitle">
                Echtzeit-Übersicht über Stimmung, Aufgaben &amp; Support-Anfragen deiner Facebook-Gruppen
            </p>
        </div>
        <div class="pill-row" style="margin:0;">
            <span class="pill pill-active">● {active_groups_count} / {len(groups)} Gruppen aktiv</span>
            <span class="pill pill-group">📊 {len(posts)} Beiträge gesamt</span>
        </div>
    </div>
    """
)

# --- Session-Ablauf-Warnung (prominent ganz oben) ---
if not session_ok or fb_session_status == "expired":
    alert_left, alert_right = st.columns([4.5, 1.5], vertical_alignment="center")
    alert_left.error(
        f"⚠️ **Facebook-Session abgelaufen!** {fb_session_msg or 'Bitte neu einloggen, damit auch private Gruppen weiter gescrapt werden.'}"
    )
    if alert_right.button("🔑 Jetzt neu einloggen", key="top_relogin_btn", type="primary", width="stretch"):
        try:
            launch_login_browser()
            st.success("Login-Browser geöffnet – nach dem Login einfach das Fenster schließen.")
        except Exception as exc:
            st.error(str(exc))

# --- KPI Strip ---
total_count = len(scope_posts)
open_actionable = sum(1 for p in scope_posts if p["is_actionable"] and p["status"] == "open")
high_prio_open = sum(
    1 for p in scope_posts if p["is_actionable"] and p["status"] == "open" and p["priority"] == "High"
)
done_count = sum(1 for p in scope_posts if p["status"] == "done")

st.html(
    f"""
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="kpi-label">Erfasste Beiträge</div>
            <div class="kpi-value">{total_count}</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Offene Aktivitäten</div>
            <div class="kpi-value kpi-accent-indigo">{open_actionable}</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Hohe Priorität</div>
            <div class="kpi-value {'kpi-accent-red' if high_prio_open > 0 else ''}">{high_prio_open}</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-label">Erledigt</div>
            <div class="kpi-value kpi-accent-green">{done_count}</div>
        </div>
    </div>
    """
)

# --- Hero Section: Per-Group Sentiment Barometer Grid ---
st.html('<div class="section-heading">🌡️ Stimmungsbarometer nach Gruppe</div>')

if not display_groups:
    st.info("Noch keine Facebook-Gruppen hinterlegt. Lege im Tab **⚙️ Gruppen & Login** deine erste Gruppe an.")
else:
    n_groups = len(display_groups)
    cols_per_row = 2 if n_groups == 4 else min(n_groups, 3)

    for row_start in range(0, n_groups, cols_per_row):
        row_groups = display_groups[row_start : row_start + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, grp in zip(cols, row_groups):
            grp_name = grp["name"]
            grp_posts = [p for p in posts if p["group_name"] == grp_name]
            is_selected = active_group == grp_name
            with col:
                with st.container(border=True):
                    render_group_barometer_card(grp, grp_posts, is_selected=is_selected)
                    btn_label = "✓ Gefiltert (Alle zeigen)" if is_selected else "Beiträge filtern"
                    if st.button(
                        btn_label,
                        key=f"select_grp_{grp['id']}",
                        type="primary" if is_selected else "secondary",
                        width="stretch",
                    ):
                        st.session_state["active_group"] = None if is_selected else grp_name
                        st.rerun()

if active_group:
    b_left, b_right = st.columns([5, 1.3], vertical_alignment="center")
    b_left.info(f"Aktiver Gruppen-Filter: **{active_group}** ({len(scope_posts)} Beiträge)")
    if b_right.button("✕ Alle Gruppen", width="stretch"):
        st.session_state["active_group"] = None
        st.rerun()

st.markdown("")

# --- Primary Tabs ---
tab_cards, tab_table, tab_settings = st.tabs(
    [
        f"📋 Aktivitäten ({len(scope_posts)})",
        "📑 Tabelle & Export",
        f"⚙️ Gruppen & Login ({len(groups)})",
    ]
)

# --- Inline Filter Toolbar (shared between Aktivitäten & Tabelle) ---
with tab_cards:
    row1_search, row1_toggle, row1_more = st.columns([3.6, 1.5, 1.1], vertical_alignment="center")
    with row1_search:
        search_query = st.text_input(
            "Suche",
            placeholder="🔍 Suchen (Titel, Details, Originaltext, Autor)...",
            label_visibility="collapsed",
        )
    with row1_toggle:
        only_actionable = st.toggle("Nur Actionable", value=False)
    with row1_more:
        with st.popover("🎛️ Filter", width="stretch"):
            all_categories = ["Termin/Event", "Frage/Support", "Bedarf/Anfrage", "To-Do", "Info"]
            selected_categories = st.pills(
                "Kategorien",
                options=all_categories,
                default=all_categories,
                selection_mode="multi",
            )
            all_priorities = ["High", "Medium", "Low"]
            selected_priorities = st.pills(
                "Prioritäten",
                options=all_priorities,
                default=all_priorities,
                selection_mode="multi",
            )

    row2_status, row2_sent = st.columns([1.0, 1.2], vertical_alignment="center")
    with row2_status:
        selected_statuses = st.segmented_control(
            "Status",
            options=["open", "done", "ignored"],
            default=["open"],
            selection_mode="multi",
            format_func=lambda s: STATUS_LABELS.get(s, s),
            label_visibility="collapsed",
        )
    with row2_sent:
        selected_sentiments = st.pills(
            "Stimmung",
            options=["Positiv", "Neutral", "Kritisch/Negativ"],
            default=None,
            selection_mode="multi",
            format_func=lambda s: SENTIMENT_LABELS.get(s, s),
            label_visibility="collapsed",
        )

    # Leere Auswahl bei Pills/Segmented Control als "Alle" behandeln
    active_statuses = set(selected_statuses or ["open", "done", "ignored"])
    active_sentiments = set(selected_sentiments or ["Positiv", "Neutral", "Kritisch/Negativ"])
    active_categories = set(selected_categories or ["Termin/Event", "Frage/Support", "Bedarf/Anfrage", "To-Do", "Info"])
    active_priorities = set(selected_priorities or ["High", "Medium", "Low"])

    filtered: List[Dict[str, Any]] = []
    q = search_query.strip().lower()
    for p in scope_posts:
        if only_actionable and not p["is_actionable"]:
            continue
        if p["status"] not in active_statuses:
            continue
        if p["sentiment"] not in active_sentiments:
            continue
        if p["category"] not in active_categories:
            continue
        if p["priority"] not in active_priorities:
            continue
        if q:
            haystack = " ".join(
                [
                    p["group_name"],
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

    # Pagination (Seitenweise blättern bei vielen Beiträgen)
    filter_signature = (
        active_group,
        tuple(sorted(active_statuses)),
        tuple(sorted(active_sentiments)),
        tuple(sorted(active_categories)),
        tuple(sorted(active_priorities)),
        only_actionable,
        q,
    )
    if st.session_state.get("cards_filter_sig") != filter_signature:
        st.session_state["cards_filter_sig"] = filter_signature
        st.session_state["cards_page"] = 1

    st.session_state.setdefault("cards_page", 1)

    p_info, p_size_col = st.columns([4.5, 1.3], vertical_alignment="center")
    with p_size_col:
        page_size = st.selectbox(
            "Pro Seite",
            options=[10, 20, 50],
            index=0,
            key="cards_page_size",
            label_visibility="collapsed",
            format_func=lambda n: f"{n} pro Seite",
        )

    total_filtered = len(filtered)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    current_page = max(1, min(int(st.session_state["cards_page"]), total_pages))
    st.session_state["cards_page"] = current_page

    start_idx = (current_page - 1) * page_size
    end_idx = min(start_idx + page_size, total_filtered)
    page_items = filtered[start_idx:end_idx]

    with p_info:
        if total_filtered == 0:
            st.caption(f"Zeige **0** von {len(scope_posts)} Beiträgen")
        else:
            st.caption(
                f"Zeige **{start_idx + 1}–{end_idx}** von **{total_filtered}** gefilterten Beiträgen "
                f"(Seite **{current_page} / {total_pages}**)"
            )

    def _render_pager(key_prefix: str) -> None:
        if total_pages <= 1:
            return
        c_prev, c_mid, c_next = st.columns([1.2, 2.6, 1.2], vertical_alignment="center")
        if c_prev.button(
            "⬅️ Zurück",
            key=f"{key_prefix}_prev",
            disabled=current_page <= 1,
            width="stretch",
        ):
            st.session_state["cards_page"] = current_page - 1
            st.rerun()
        c_mid.html(
            f'<div style="text-align:center; font-size:0.85rem; font-weight:600; color:#475569;">'
            f"Seite {current_page} von {total_pages}</div>"
        )
        if c_next.button(
            "Weiter ➡️",
            key=f"{key_prefix}_next",
            disabled=current_page >= total_pages,
            width="stretch",
        ):
            st.session_state["cards_page"] = current_page + 1
            st.rerun()

    if not page_items:
        st.info("Keine Beiträge entsprechen den aktuellen Filterkriterien.")
    else:
        _render_pager("pager_top")
        for p in page_items:
            render_activity_card(p)
        _render_pager("pager_bottom")

with tab_table:
    if not filtered:
        st.info("Keine Daten zum Anzeigen.")
    else:
        table_rows = [
            {
                "Zeitpunkt": format_timestamp(p["timestamp"]),
                "Gruppe": p["group_name"],
                "Relevant": "Ja" if p["is_actionable"] else "Nein",
                "Status": STATUS_LABELS.get(p["status"], p["status"]),
                "Kategorie": p["category"],
                "Priorität": p["priority"],
                "Stimmung": f"{SENTIMENT_LABELS.get(p['sentiment'], p['sentiment'])} ({p['sentiment_score']:+d})",
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
            width="stretch",
            hide_index=True,
            column_config={
                "Link": st.column_config.LinkColumn("Facebook-Link", display_text="Öffnen ↗"),
            },
        )
        st.download_button(
            label="⬇️ Gefilterte Liste als CSV herunterladen",
            data=to_csv_bytes(filtered),
            file_name="facebook_activities.csv",
            mime="text/csv",
        )

with tab_settings:
    st.subheader("👥 Facebook-Gruppen & -Seiten verwalten")
    st.caption(
        "Alle aktiven Einträge werden beim nächsten Durchlauf automatisch nacheinander in derselben Browser-Session gescrapt."
    )

    with st.form("add_group_form", clear_on_submit=True):
        f_col1, f_col2, f_col3 = st.columns([2, 3, 1.2], vertical_alignment="bottom")
        new_name = f_col1.text_input("Name", placeholder="z. B. Kunden-Community")
        new_url = f_col2.text_input(
            "Facebook-Gruppen-URL",
            placeholder="https://www.facebook.com/groups/1234567890",
        )
        submitted = f_col3.form_submit_button("➕ Hinzufügen", type="primary", width="stretch")
        if submitted:
            if not new_url.strip():
                st.error("Bitte eine gültige Facebook-URL eingeben.")
            else:
                try:
                    db.add_group(new_name, new_url)
                    st.cache_data.clear()
                    st.success(f"Gruppe '{new_name or new_url}' hinzugefügt!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("Diese URL ist bereits in der Liste vorhanden.")
                except Exception as exc:
                    st.error(str(exc))

    if not groups:
        st.info("Noch keine Gruppen hinterlegt. Füge oben deine erste Facebook-Gruppe hinzu.")
    else:
        for grp in groups:
            with st.container(border=True):
                g_left, g_actions = st.columns([4, 2], vertical_alignment="center")
                with g_left:
                    state_badge = (
                        '<span class="pill pill-active">● Aktiv</span>'
                        if grp["enabled"]
                        else '<span class="pill pill-paused">⏸ Pausiert</span>'
                    )
                    st.html(
                        f"""
                        <div style="display:flex; align-items:center; gap:8px; margin-bottom:2px;">
                            <strong style="font-size:1rem; color:#0F172A;">{_esc(grp['name'])}</strong>
                            {state_badge}
                        </div>
                        <div style="font-size:0.8rem; color:#64748B;">
                            {_esc(grp['url'])} · Letzter Check: {_esc(format_timestamp(grp.get('last_scraped_at') or ''))}
                        </div>
                        """
                    )
                with g_actions:
                    b1, b2 = st.columns(2)
                    if grp["enabled"]:
                        if b1.button("⏸️ Pausieren", key=f"pause_g_{grp['id']}", width="stretch"):
                            db.update_group(grp["id"], enabled=False)
                            st.cache_data.clear()
                            st.rerun()
                    else:
                        if b1.button("▶️ Aktivieren", key=f"enable_g_{grp['id']}", width="stretch"):
                            db.update_group(grp["id"], enabled=True)
                            st.cache_data.clear()
                            st.rerun()

                    if b2.button("🗑️ Entfernen", key=f"del_g_{grp['id']}", width="stretch"):
                        db.delete_group(grp["id"])
                        st.cache_data.clear()
                        st.rerun()

                with st.expander("✏️ Name oder URL bearbeiten"):
                    with st.form(f"edit_group_{grp['id']}"):
                        e1, e2, e3 = st.columns([2, 3, 1.2], vertical_alignment="bottom")
                        edit_name = e1.text_input("Name", value=grp["name"])
                        edit_url = e2.text_input("URL", value=grp["url"])
                        if e3.form_submit_button("💾 Speichern", width="stretch"):
                            try:
                                db.update_group(grp["id"], name=edit_name, url=edit_url)
                                st.cache_data.clear()
                                st.success("Gespeichert!")
                                st.rerun()
                            except sqlite3.IntegrityError:
                                st.error("Diese URL existiert bereits.")
                            except Exception as exc:
                                st.error(str(exc))

    st.divider()
    st.subheader("🔑 Facebook-Login & Session")
    if session_ok:
        st.success(f"Gespeicherte Browser-Session aktiv: `{SESSION_DIR}`")
    else:
        st.warning(f"Noch keine Session in `{SESSION_DIR}` gefunden. Bitte zuerst einloggen.")

    st.write(
        "Wenn Facebook einen neuen Login, eine 2FA-Bestätigung oder Cookie-Zustimmung verlangt, "
        "klicke auf den Button unten. Es öffnet sich ein sichtbares Chromium-Fenster – "
        "nach dem Login einfach das Browser-Fenster schließen, die Session wird automatisch gespeichert."
    )
    if st.button("🔑 Facebook-Login im Browser öffnen", type="primary"):
        try:
            launch_login_browser()
            st.success(
                "Browser-Fenster wird gestartet! Bitte bei Facebook einloggen und das Fenster danach schließen."
            )
        except Exception as exc:
            st.error(f"Fehler beim Starten von auth_setup.py: {exc}")
