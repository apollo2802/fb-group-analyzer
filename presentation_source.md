# Facebook Group Analyzer – Automatisierte Gruppen-Überwachung & KI-Auswertung

## 1. Ausgangssituation & Problemstellung
In vielen Unternehmen, Vereinen, Communities und Projektteams laufen wichtige Anfragen, Support-Fälle, Termine und Bedarfe über geschlossene Facebook-Gruppen. Das manuelle Mitlesen ist zeitintensiv, fehleranfällig und führt häufig dazu, dass dringende Fragen oder Deadlines übersehen werden – insbesondere, wenn relevante Informationen erst in den Kommentaren unter einem Beitrag entstehen.

## 2. Ziel der Lösung
Der **Facebook Group Analyzer** automatisiert die komplette Kette von der Datenerfassung über die strukturierte KI-Bewertung bis zur übersichtlichen Aufgabenverwaltung:
- Kontinuierliche, automatische Überwachung einer Facebook-Gruppe (Standard: alle 20 Minuten)
- Zuverlässige Erfassung neuer Beiträge **und** neuer Kommentare ohne doppelte Verarbeitung
- Datenschutzkonforme (DSGVO) Anonymisierung personenbezogener Daten vor der KI-Analyse
- Automatische Ableitung strukturierter, handlungsrelevanter Aktivitäten (Kategorie, Priorität, Titel, Details, Fälligkeitsdatum)
- Zentrale Übersicht im interaktiven **Streamlit-Dashboard** sowie Export als CSV oder per Webhook (z. B. Teams, E-Mail, n8n, Make)

## 3. Technische Architektur (5 Schichten)

### Schicht 1: Externe Datenquelle
- Zielgruppe auf Facebook (`FB_GROUP_URL`) mit chronologischer Sortierung (`?sorting_setting=CHRONOLOGICAL`), damit brandneue Beiträge zuverlässig ganz oben erscheinen.

### Schicht 2: Datenerfassung & Orchestrierung (`auth_setup.py`, `scraper.py`, `main.py`)
- **Einmaliger manueller Login (`auth_setup.py`):** Startet Chromium sichtbar (`headless=False`) und speichert Cookies sowie Login-Status dauerhaft im lokalen Profilordner `./fb_session`. Dadurch werden automatisierte Login-Sicherheitsabfragen und 2FA-Probleme vermieden.
- **Robuste Extraktion (`scraper.py`):** Statt fragiler CSS-Selektoren kombiniert der Scraper zwei zuverlässige Datenquellen:
  1. Auslesen der initial serverseitig gerenderten (SSR) JSON-Blöcke (`<script type="application/json">`) für die allerneuesten Beiträge ganz oben.
  2. Abfangen des laufenden `/api/graphql/`-Netzwerkverkehrs beim langsamen Scrollen über Playwright (`page.on("response")`).
- **Orchestrierung (`main.py`):** Ein `APScheduler`-Job führt die Pipeline regelmäßig (z. B. alle 20 Minuten) vollautomatisch im Hintergrund aus.

### Schicht 3: Datenhaltung & Deduplizierung (`db.py`, `state.db`)
- Lokale SQLite-Datenbank (`state.db`) speichert `post_id`, Autor, Inhalt, Kommentare, Zeitstempel, Kommentaranzahl, Permalink, KI-Auswertung (`extracted_actions`) und Bearbeitungsstatus (`open`, `done`, `ignored`).
- **Intelligente Deduplizierung:** Bereits bekannte Beiträge werden nicht erneut an die KI gesendet – außer es sind neue Kommentare oder Textänderungen hinzugekommen. Das spart API-Kosten und verhindert doppelte Aufgaben.

### Schicht 4: KI-Analyse & Datenschutz (`analyzer.py`)
- **DSGVO-PII-Anonymisierung (`ANONYMIZE_PII=true`):** Bevor Texte an das Cloud-LLM übermittelt werden, maskiert das Skript automatisch Klarnamen (`[Autor]`, `[Kommentator N]`, `[Person_N]`), E-Mail-Adressen (`[E-MAIL]`), Telefonnummern (`[TELEFON]`) und Facebook-Profil-URLs (`[PROFIL-URL]`).
- **Structured Outputs mit Google Gemini & Pydantic:** Das Modell (`gemini-2.5-flash`) liefert garantiert valides JSON gemäß dem Schema `DerivedActivity`:
  - `is_actionable` (bool): Liegt eine konkrete Aufgabe, Frage, Anfrage oder ein Termin vor?
  - `category`: `Termin/Event` | `Frage/Support` | `Bedarf/Anfrage` | `To-Do` | `Info`
  - `action_title`: Kurzer, prägnanter Titel
  - `details`: Kontext aus Beitrag und Kommentaren (anonymisiert)
  - `due_date`: ISO-Datum (`YYYY-MM-DD`), falls zeitlicher Bezug erkennbar
  - `priority`: `High` | `Medium` | `Low`

### Schicht 5: Präsentation, Export & Integration (`dashboard.py`, `dispatcher.py`)
- **Interaktives Streamlit-Dashboard (`dashboard.py` auf `localhost:8501`):**
  - KPI-Karten auf einen Blick (Erfasste Beiträge, Offene Aktivitäten, Hohe Priorität, Erledigt)
  - Filter nach Relevanz (`is_actionable`), Kategorie, Priorität, Status und Volltextsuche
  - Direktes Abhaken (`Erledigt` / `Ignorieren` / `Wieder öffnen`) mit Speicherung in `state.db`
  - Aufklappbarer Original-Beitrag samt Kommentaren und Direktlink zum Facebook-Post
  - Tabellenansicht mit CSV-Download sowie Button zum manuellen Anstoßen der Pipeline
- **Dispatcher (`dispatcher.py`):** Schreibt ausgewertete Aktivitäten fortlaufend in `activities.csv` und kann relevante Einträge (`is_actionable=True`) optional per JSON-POST an eine `WEBHOOK_URL` (z. B. Microsoft Teams Workflows, n8n, Make, Zapier oder E-Mail-Bridge) weiterleiten.

## 4. Praxis-Ergebnisse & Verifikation
- Erfolgreicher End-to-End-Test an einer realen Facebook-Testgruppe:
  - 6 Beiträge inkl. zugehöriger Kommentare und Permalinks automatisch extrahiert
  - Testbeiträge zuverlässig als `Info / Low / is_actionable=False` herausgefiltert
  - Echter Support-Beitrag sofort als `Frage/Support / High / is_actionable=True` erkannt und priorisiert
  - Deduplizierung verhindert doppelte Verarbeitung bei Folgeläufen (0 redundante API-Calls)

## 5. Betrieb, Skalierung & Ausblick
- **Lokal:** Läuft direkt auf macOS/Linux/Windows im Hintergrund (Scheduler + lokales Dashboard).
- **24/7-Betrieb:** Leicht containerisierbar (Docker) für Raspberry Pi, Heimserver oder Cloud-VM.
- **Mögliche Erweiterungen:** Direkte Push-Benachrichtigungen an Microsoft Teams, E-Mail-Zusammenfassungen (SMTP), Anbindung an Ticketsysteme/Planner (Jira, Notion, Todoist) sowie paralleles Monitoring mehrerer Facebook-Gruppen.
