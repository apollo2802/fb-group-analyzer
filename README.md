# Facebook Group Analyzer

Automatisierte Pipeline zum Überwachen einer Facebook-Gruppe mit Playwright. SQLite speichert Beiträge und Kommentare. Optional wertet Gemini die Texte als strukturierte Aktivitäten aus, die sich per CSV und Webhook exportieren lassen.

## Projektstruktur

- `.env` – API-Keys (`GEMINI_API_KEY`), Gruppen-URL (`FB_GROUP_URL`) und Konfiguration
- `db.py` / `state.db` – SQLite-Datenbank zur Deduplizierung (`post_id`, `comment_count`, `extracted_actions`)
- `auth_setup.py` – Einmaliger manueller Login mit persistentem Browser-Profil (`./fb_session`)
- `scraper.py` – Playwright-Scraper mit `/api/graphql/`-Interception
- `analyzer.py` – optionale Gemini-Auswertung und Maskierung typischer Identifikatoren
- `dispatcher.py` – Export abgeleiteter Aktivitäten in `activities.csv` und optional an einen Webhook
- `main.py` – Orchestrierung & APScheduler (Standard: alle 20 Minuten)

## Einrichtung & Nutzung

### 1. Umgebung einrichten
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

### 2. `.env` konfigurieren
Kopiere `.env.example` nach `.env` und trage `FB_GROUP_URL` ein. Für die Cloud-Analyse sind zusätzlich `GEMINI_ALLOW_CLOUD=true` und ein `GEMINI_API_KEY` erforderlich. Bestehende Installationen müssen die Freigabe ebenfalls setzen. Bis dahin sammelt die Pipeline weiter Beiträge und hält neue Analysen zurück.

Vor dem Versand an Gemini maskiert der Analyzer bekannte Autorennamen, E-Mail-Adressen, typische Telefonnummern und Weblinks. Freitext kann weiterhin Personen identifizieren. SQLite und das Dashboard enthalten die Originaldaten. Bei aktiviertem Webhook werden die Auswertung und der Link zum Originalbeitrag an den konfigurierten Empfänger übertragen. Debug-Dumps können Rohdaten enthalten. Schütze Datenbank, Browserprofil, Exporte und Debug-Dumps entsprechend.

### 3. Einmaliger manueller Facebook-Login
```bash
python auth_setup.py
```
Logge dich im sichtbaren Chromium-Fenster ein, bestätige Cookies/2FA, öffne die Zielgruppe und drücke im Terminal `Enter`. Die Session wird lokal in `./fb_session` gespeichert.

### 4. Testlauf starten
```bash
# Einzelner Durchlauf mit sichtbarem Browser
python main.py --once --visible

# Oder nur Scraper testen
python main.py --scrape-only --visible

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
Starte den Befehl aus dem Projektverzeichnis, damit Streamlit die lokale Bindung an `127.0.0.1` lädt. Das Dashboard ist unter `http://127.0.0.1:8501` erreichbar. Für Fernzugriff verwende einen SSH-Tunnel oder einen authentifizierten Reverse-Proxy.

Der CSV-Export behält seine bisherigen Spalten. Wiederholte Zustellversuche verwenden denselben Exportzeitpunkt und dieselbe Webhook-ID. Ein Webhook-Empfänger kann Wiederholungen anhand des Headers `Idempotency-Key` erkennen.
