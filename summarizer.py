#!/usr/bin/env python3
"""
Doppelgänger Podcast Summarizer
Fetches new episodes, gets transcripts (doppelgaenger.ai or Whisper),
summarizes with Claude, and sends via email.
"""

import os
import json
import smtplib
import logging
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import re

import feedparser
import requests
from bs4 import BeautifulSoup
import anthropic

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

RSS_FEED = "https://feeds.megaphone.fm/LINDALA4208458418"
TRANSCRIPT_BASE = "https://doppelgaenger.ai/podcast/"
STATE_FILE = Path("last_processed.json")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_processed_id": None, "last_processed_date": None}


def save_state(episode_id: str, episode_date: str) -> None:
    STATE_FILE.write_text(
        json.dumps({"last_processed_id": episode_id, "last_processed_date": episode_date}, indent=2)
    )


# ---------------------------------------------------------------------------
# RSS Feed
# ---------------------------------------------------------------------------

MAX_EPISODES_PER_RUN = 2


def fetch_new_episodes(last_id: str | None) -> list[dict]:
    """Return all episodes newer than last_id, oldest first.

    Walks the feed from newest to oldest and stops as soon as it hits the
    last episode we already processed. The result is reversed so callers can
    process the oldest unprocessed episode first (and never skip one). The
    per-run cap is applied by the caller, not here, so that a backlog drains
    in chronological order instead of dropping the oldest entries.
    """
    log.info("Fetching RSS feed …")
    feed = feedparser.parse(RSS_FEED)
    # bozo is set for minor issues (e.g. encoding warnings); only fail on real errors
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS parse error: {feed.bozo_exception}")

    episodes = []
    for entry in feed.entries:
        ep_id = entry.get("id") or entry.get("guid") or entry.get("link")
        if ep_id == last_id:
            break
        audio_url = None
        for link in entry.get("links", []):
            if link.get("type", "").startswith("audio/"):
                audio_url = link["href"]
                break
        if not audio_url:
            enclosures = entry.get("enclosures", [])
            if enclosures:
                audio_url = enclosures[0].get("url")

        pub_date = entry.get("published", "")
        episodes.append({
            "id": ep_id,
            "title": entry.get("title", "Unbekannte Episode"),
            "published": pub_date,
            "audio_url": audio_url,
            "summary": entry.get("summary", ""),
        })

    episodes.reverse()  # process oldest first
    log.info(f"Found {len(episodes)} new episode(s)")
    return episodes


# ---------------------------------------------------------------------------
# Transcript Fetching
# ---------------------------------------------------------------------------

def _parse_date_from_published(published: str) -> str | None:
    """Extract YYYY-MM-DD from RSS published string."""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(published, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Try ISO format
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    return None


_UMLAUT_MAP = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}


def _transliterate(text: str) -> str:
    for k, v in _UMLAUT_MAP.items():
        text = text.replace(k, v)
    return text


def _build_transcript_url(date_str: str, title: str) -> str:
    """Build doppelgaenger.ai URL from date and episode title.

    The site uses slugs like: 2024-03-07_Episode_Title_Here
    Umlauts are transliterated and special chars replaced with underscores.
    """
    slug = _transliterate(title)
    slug = slug.replace(" ", "_").replace("/", "_").replace("|", "_").replace("-", "_")
    slug = "".join(c for c in slug if c.isalnum() or c == "_")
    # Collapse multiple underscores and strip trailing ones
    slug = re.sub(r"_+", "_", slug).strip("_")
    slug = slug[:60]
    return f"{TRANSCRIPT_BASE}{date_str}_{slug}"


def fetch_transcript_from_site(episode: dict) -> str | None:
    """Try to scrape transcript from doppelgaenger.ai."""
    date_str = _parse_date_from_published(episode["published"])
    if not date_str:
        log.warning("Could not parse date from: %s", episode["published"])
        return None

    url = _build_transcript_url(date_str, episode["title"])
    log.info(f"Trying transcript URL: {url}")

    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        if resp.status_code == 404:
            log.info("Transcript not found on doppelgaenger.ai (404)")
            return None
        if resp.status_code != 200:
            log.warning(f"doppelgaenger.ai returned {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Try various selectors that might contain transcript text
        for selector in [
            "article", ".transcript", "#transcript",
            ".content", "main", ".episode-content",
        ]:
            block = soup.select_one(selector)
            if block:
                text = block.get_text(separator="\n", strip=True)
                if len(text) > 500:
                    log.info(f"Got transcript ({len(text)} chars) via selector '{selector}'")
                    return text

        # Fallback: body text
        body = soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            if len(text) > 500:
                log.info(f"Got transcript ({len(text)} chars) via body fallback")
                return text

    except requests.RequestException as e:
        log.warning(f"Request error for {url}: {e}")

    return None


def transcribe_with_whisper(audio_url: str) -> str | None:
    """Download audio and transcribe with OpenAI Whisper API."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set — skipping Whisper transcription")
        return None

    import openai  # optional dependency
    client = openai.OpenAI(api_key=api_key)

    log.info(f"Downloading audio from {audio_url} …")
    audio_path = Path("/tmp/episode_audio.mp3")
    with requests.get(audio_url, stream=True, timeout=60, headers=BROWSER_HEADERS) as r:
        r.raise_for_status()
        with open(audio_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    log.info("Transcribing with Whisper …")
    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="de",
        )
    audio_path.unlink(missing_ok=True)
    return result.text


def get_transcript(episode: dict) -> tuple[str, str]:
    """Return (transcript_text, source_label)."""
    transcript = fetch_transcript_from_site(episode)
    if transcript:
        return transcript, "doppelgaenger.ai"

    if episode.get("audio_url"):
        transcript = transcribe_with_whisper(episode["audio_url"])
        if transcript:
            return transcript, "OpenAI Whisper"

    # Fallback: use RSS description
    desc = episode.get("summary", "")
    if desc:
        log.warning("Using RSS description as fallback (no full transcript)")
        return desc, "RSS-Beschreibung (kein Volltranskript)"

    return "", "keine Quelle verfügbar"


# ---------------------------------------------------------------------------
# Summarization with Claude
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """Du bekommst das Transkript einer Doppelgänger Tech Talk Podcast-Folge.
Erstelle eine strukturierte, deutschsprachige Zusammenfassung.

Der Podcast hat wiederkehrende Rubriken. Ordne die Themen in diese Überkapitel ein,
ABER nur wenn sie in dieser Folge tatsächlich vorkommen (nicht erzwingen):

- **Earnings** — Analyse von Quartalszahlen und Geschäftsberichten
- **Schmuddelecke** — Kontroverse, ethisch fragwürdige oder skandalöse Themen

Themen die in keine Rubrik passen kommen unter "Weitere Themen".

## Gewünschtes Format

### ⚡ Das Wichtigste in 8 Sätzen
Eine kompakte Zusammenfassung der Folge für jemanden der nur 2–3 Minuten Zeit hat.
Nenne die wichtigsten Themen und die zentralen Positionen der Hosts.

### 📊 Earnings (nur wenn in der Folge vorhanden)
Für jedes besprochene Unternehmen:

#### **Unternehmensname**
- Wichtigste Zahlen und Fakten (Umsatz, Gewinn, Wachstum, Guidance etc.)
- Kontext und Einordnung: Wie stehen die Zahlen im Vergleich zu Erwartungen oder Vorquartal?
- Diskussionsverlauf: Welche Aspekte wurden besprochen, worüber waren sich die Hosts einig/uneinig?
- Meinungen & Kernaussagen:
  - Was sagt Pip dazu? (konkrete Einschätzung, Argumentation)
  - Was sagt Glöckner dazu? (konkrete Einschätzung, Argumentation)

### 🚨 Schmuddelecke (nur wenn in der Folge vorhanden)
Für jedes Thema:

#### **Thema**
- Infos, Fakten und Hintergründe
- Was genau ist passiert oder wurde aufgedeckt?
- Diskussionsverlauf: Wie haben die Hosts das Thema aufgerollt?
- Meinungen & Kernaussagen der Hosts (mit Zuordnung wer was gesagt hat)

### 🎙️ Weitere Themen
Für jedes Thema:

#### **Thema-Titel**
- Infos: Wichtigste Fakten, Zahlen, Hintergründe (als Stichpunkte)
- Kontext: Warum ist das relevant, was ist der größere Zusammenhang?
- Diskussionsverlauf: Welche Teilaspekte wurden besprochen, wie hat sich die Diskussion entwickelt?
- Meinungen & Kernaussagen:
  - Wer hat was gesagt (Pip / Glöckner), konkrete Einschätzungen und Argumentation
  - Wo waren sie sich einig, wo gab es unterschiedliche Perspektiven?

## Regeln
- Schreibe ausführlich genug, dass man den Diskussionsverlauf und die Argumente gut nachvollziehen kann
- Fokus auf Fakten, Zahlen, konkrete Meinungen und die Argumentation dahinter
- Pro Thema 5–9 Stichpunkte für Infos und Kontext, 2–5 für Meinungen
- Gib die Positionen von Pip und Glöckner möglichst getrennt und mit ihren jeweiligen Begründungen wieder
- Wenn die Hosts Vorhersagen, Empfehlungen oder konkrete Ratschläge geben, halte diese fest
- Verwende Markdown: ##/### für Überschriften, #### für Themen, - für Listen, **fett** für Hervorhebungen"""


MODELS = [
    ("claude-sonnet-4-6", "Sonnet"),
]


def summarize_with_claude(transcript: str, episode_title: str, model: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    # Truncate transcript to ~100k chars to stay within context limits
    truncated = transcript[:100_000]
    if len(transcript) > 100_000:
        truncated += "\n\n[Transkript wurde auf 100.000 Zeichen gekürzt]"

    log.info(f"Summarizing episode '{episode_title}' with {model} …")
    message = client.messages.create(
        model=model,
        max_tokens=12000,  # generous limit for detailed summaries
        messages=[
            {
                "role": "user",
                "content": f"{SUMMARY_PROMPT}\n\n---\nEpisodentitel: {episode_title}\n\nTranskript:\n{truncated}",
            }
        ],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Email Sending
# ---------------------------------------------------------------------------

def send_email(subject: str, html_body: str, text_body: str) -> None:
    smtp_host = os.environ.get("EMAIL_SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("EMAIL_SMTP_PORT") or "587")
    email_from = os.environ.get("EMAIL_FROM")
    email_to = os.environ.get("EMAIL_TO")
    email_password = os.environ.get("EMAIL_PASSWORD")

    if not all([email_from, email_to, email_password]):
        raise RuntimeError("EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD must be set")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    log.info(f"Sending email to {email_to} …")
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(email_from, email_password)
        server.sendmail(email_from, email_to, msg.as_string())
    log.info("Email sent successfully")


def _markdown_to_html(text: str) -> str:
    """Convert markdown summary to email-safe HTML.

    Supports:
    - Headings: # through ####
    - Top-level list items: - or * (no leading spaces)
    - Indented sub-items:   - or   * (2+ leading spaces)
    - Bold: **text**
    """
    lines = text.split("\n")
    html_lines: list[str] = []
    in_list = False
    in_sublist = False

    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()

        # Headings: ## or ###
        heading_match = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if heading_match:
            if in_sublist:
                html_lines.append("</ul>")
                in_sublist = False
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            level = len(heading_match.group(1))
            tag = f"h{min(level + 1, 4)}"  # ## -> h3, ### -> h4
            html_lines.append(f"<{tag}>{heading_match.group(2)}</{tag}>")
            continue

        # Indented sub-list items (2+ leading spaces before - or *)
        sublist_match = re.match(r"^ {2,}[-*]\s+(.*)", raw)
        if sublist_match:
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            if not in_sublist:
                html_lines.append("  <ul>")
                in_sublist = True
            item = sublist_match.group(1)
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            html_lines.append(f"    <li>{item}</li>")
            continue

        # Top-level list items: - or *
        list_match = re.match(r"^[-*]\s+(.*)", stripped)
        if list_match:
            if in_sublist:
                html_lines.append("  </ul>")
                in_sublist = False
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            item = list_match.group(1)
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            html_lines.append(f"  <li>{item}</li>")
            continue

        # Close lists if we hit a non-list line
        if in_sublist:
            html_lines.append("  </ul>")
            in_sublist = False
        if in_list and not stripped:
            html_lines.append("</ul>")
            in_list = False

        if not stripped:
            html_lines.append("")
            continue

        # Regular paragraph — also handle bold
        para = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
        html_lines.append(f"<p>{para}</p>")

    if in_sublist:
        html_lines.append("  </ul>")
    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def build_email(episode: dict, summary: str, transcript_source: str, model_label: str = "Sonnet") -> tuple[str, str, str]:
    """Return (subject, html, plain_text)."""
    title = episode["title"]
    published = episode.get("published", "")
    subject = f"🎙️ [{model_label}] Doppelgänger Zusammenfassung: {title}"

    html_summary = _markdown_to_html(summary)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #333; }}
    h1 {{ color: #1a1a2e; border-bottom: 2px solid #e94560; padding-bottom: 10px; }}
    h3 {{ color: #16213e; margin-top: 24px; }}
    h4 {{ color: #2a4a7f; margin-top: 18px; margin-bottom: 6px; }}
    ul {{ margin: 8px 0; padding-left: 24px; }}
    li {{ margin-bottom: 4px; line-height: 1.5; }}
    .meta {{ color: #666; font-size: 0.9em; margin-bottom: 20px; }}
    .source {{ background: #f0f0f0; padding: 4px 8px; border-radius: 4px; font-size: 0.8em; }}
    a {{ color: #e94560; }}
  </style>
</head>
<body>
  <h1>🎙️ Doppelgänger Tech Talk</h1>
  <p class="meta">
    <strong>{title}</strong><br>
    Veröffentlicht: {published}<br>
    Transkript-Quelle: <span class="source">{transcript_source}</span>
  </p>
  <hr>
  {html_summary}
  <hr>
  <p style="color:#999;font-size:0.8em;">
    Automatisch erstellt mit Claude {model_label} &amp; dem Doppelgänger Podcast Summarizer<br>
    <a href="https://www.doppelgaenger.io">doppelgaenger.io</a>
  </p>
</body>
</html>"""

    plain = f"Doppelgänger Tech Talk: {title}\n{published}\nQuelle: {transcript_source}\n\n{'='*60}\n\n{summary}"
    return subject, html, plain


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    state = load_state()
    last_id = state.get("last_processed_id")

    new_episodes = fetch_new_episodes(last_id)
    if not new_episodes:
        log.info("No new episodes. Nothing to do.")
        return

    # First run ever (no persisted state). Don't email the entire back catalogue:
    # just record the newest episode as the baseline. From the next run on, only
    # genuinely new episodes are summarized and sent — exactly one mail each.
    if last_id is None:
        newest = new_episodes[-1]
        save_state(newest["id"], newest["published"])
        log.info(
            "First run — baseline set to '%s' (%s). No mails sent; future "
            "episodes will be summarized individually.",
            newest["title"], newest["published"],
        )
        return

    # Throttle: process the OLDEST unprocessed episodes first, at most
    # MAX_EPISODES_PER_RUN per run. State advances to the newest episode we
    # actually sent, so a backlog drains in order across runs without skips
    # and without ever re-sending an episode.
    batch = new_episodes[:MAX_EPISODES_PER_RUN]
    if len(new_episodes) > MAX_EPISODES_PER_RUN:
        log.info(
            "%d new episodes found; processing the oldest %d this run, the rest "
            "follow next run.", len(new_episodes), MAX_EPISODES_PER_RUN,
        )

    for episode in batch:
        log.info(f"Processing: {episode['title']}")
        transcript, source = get_transcript(episode)

        if not transcript:
            log.error(f"No transcript available for: {episode['title']}")
            continue

        for model_id, model_label in MODELS:
            summary = summarize_with_claude(transcript, episode["title"], model=model_id)
            subject, html, plain = build_email(episode, summary, source, model_label=model_label)
            send_email(subject, html, plain)
            time.sleep(2)  # be polite between API calls

        # Persist immediately after each episode so a crash mid-batch never
        # causes the just-sent episode to be re-sent on the next run.
        save_state(episode["id"], episode["published"])
        log.info(f"Done: {episode['title']}")

    log.info("All episodes processed.")


if __name__ == "__main__":
    main()
