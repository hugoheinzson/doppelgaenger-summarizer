# Doppelgänger Podcast Summarizer

Automatisches Transkribieren und Zusammenfassen neuer Doppelgänger Tech Talk Episoden, 2x pro Woche per E-Mail zugestellt.

## Funktionsweise

```
RSS Feed (feeds.megaphone.fm)
        ↓
  Neue Episoden erkennen
        ↓
  Transkript holen (doppelgaenger.ai)
  ↳ Fallback: OpenAI Whisper API
        ↓
  Zusammenfassung mit Claude API
        ↓
  E-Mail versenden
```

## Setup

### 1. Repository forken / klonen

### 2. GitHub Secrets einrichten

Gehe zu `Settings → Secrets and variables → Actions` und füge hinzu:

| Secret | Beschreibung |
|--------|-------------|
| `ANTHROPIC_API_KEY` | API Key von [console.anthropic.com](https://console.anthropic.com) |
| `EMAIL_FROM` | Absender-E-Mail (z.B. deine Gmail-Adresse) |
| `EMAIL_TO` | Empfänger-E-Mail |
| `EMAIL_PASSWORD` | App-Passwort (bei Gmail: 2FA aktivieren → App-Passwort erstellen) |
| `EMAIL_SMTP_HOST` | SMTP-Server (Standard: `smtp.gmail.com`) |
| `EMAIL_SMTP_PORT` | SMTP-Port (Standard: `587`) |
| `OPENAI_API_KEY` | *(Optional)* Nur nötig wenn doppelgaenger.ai nicht verfügbar |

### 3. Gmail App-Passwort erstellen (empfohlen)

1. Google-Konto → Sicherheit → 2-Faktor-Authentifizierung aktivieren
2. Sicherheit → App-Passwörter → Neues App-Passwort für "Mail" erstellen
3. Den 16-stelligen Code als `EMAIL_PASSWORD` eintragen

### 4. Workflow aktivieren

Der Workflow läuft automatisch:
- **Mittwoch** um 10:00 UTC
- **Samstag** um 10:00 UTC

Oder manuell via `Actions → Doppelgänger Podcast Digest → Run workflow`.

## Lokales Testen

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export EMAIL_FROM="dich@gmail.com"
export EMAIL_TO="dich@gmail.com"
export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"

python summarizer.py
```

## Was die E-Mail enthält

- **Themen der Folge** — Stichpunktliste
- **Kernaussagen & Meinungen** — Was denken Pip & Glöck?
- **Unternehmen & Produkte** — Genannte Namen mit Einordnung
- **Das Wichtigste in 3 Sätzen** — Für den schnellen Überblick

## Transkript-Quellen

1. **doppelgaenger.ai** — Inoffizielle Site mit KI-Transkripten aller Folgen (primär)
2. **OpenAI Whisper** — Audio-Transkription als Fallback (~$1/Folge)
3. **RSS-Beschreibung** — Letzter Fallback (nur Kurzbeschreibung)
