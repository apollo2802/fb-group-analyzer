# Facebook Group Analyzer

Automatisierte Pipeline zum Überwachen einer Facebook-Gruppe via Playwright GraphQL-Interception, Deduplizierung in SQLite (`state.db`), KI-Auswertung mit Gemini Structured Outputs (`DerivedActivity`) inkl. DSGVO-Anonymisierung sowie Export nach CSV/Webhook.

## Projektstruktur

- `.env` – API-Keys (`GEMINI_API_KEY`), Gruppen-URL (`FB_GROUP_URL`) und Konfiguration
- `db.py` / `state.db` – SQLite-Datenbank zur Deduplizierung (`post_id`, `comment_count`, `extracted_actions`)
- `auth_setup.py` – Einmaliger manueller Login mit persistentem Browser-Profil (`./fb_session`)
- `scraper.py` – Playwright-Scraper mit `/api/graphql/`-Interception
- `analyzer.py` – KI-Auswertung mit `google-genai`, Pydantic `DerivedActivity` und PII-Anonymisierung
- `dispatcher.py` – Export abgeleiteter Aktivitäten in `activities.csv` und optional an einen Webhook
- `main.py` – Orchestrierung & APScheduler (Standard: alle 20 Minuten)

## Einrichtung & Nutzung

### 1. Virtuelle Umgebung aktivieren (falls nicht automatisch erfolgt)
```bash
source .venv/bin/activate
```

### 2. `.env` konfigurieren
Trage mindestens `GEMINI_API_KEY` und `FB_GROUP_URL` in `.env` ein.

### 3. Einmaliger manueller Facebook-Login
```bash
python auth_setup.py
```
Logge dich im sichtbaren Chromium-Fenster ein, bestätige Cookies/2FA, öffne die Zielgruppe und drücke im Terminal `Enter`. Die Session wird lokal in `./fb_session` gespeichert.

### 4. Testlauf starten
```bash
# Einzelner Durchlauf (sichtbar + Debug-Dump der GraphQL-Responses)
python main.py --once --visible --debug-graphql

# Oder nur Scraper testen
python main.py --scrape-only --visible --debug-graphql

# Datenbank-Status prüfen
python main.py --stats
```

### 5. Dauerbetrieb (Scheduler)
```bash
python main.py
```

### 6. Streamlit-Dashboard starten
```bash
.venv/bin/streamlit run dashboard.py
```
Öffnet das lokale Dashboard im Browser (Standard: `http://localhost:8501`) zum Filtern, Suchen, Abhaken von Aktivitäten und manuellen Starten der Pipeline.
