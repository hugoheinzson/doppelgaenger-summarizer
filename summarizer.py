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

def fetch_new_episodes(last_id: str | None, max_on_first_run: int = 2) -> list[dict]:
    """Return episodes newer than last_id, oldest first.

    On the very first run (no state), only return the most recent
    `max_on_first_run` episodes to avoid processing the entire archive.
    """
    log.info("Fetching RSS feed …")
    feed = feedparser.parse(RSS_FEED)
    # bozo is set for minor issues (e.g. encoding warnings); only fail on real errors
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS parse error: {feed.bozo_exception}")

    is_first_run = last_id is None
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

    # Feed is newest-first; on first run take only the most recent N
    if is_first_run and len(episodes) > max_on_first_run:
        log.info(
            f"First run: limiting to {max_on_first_run} most recent episodes "
            f"(skipping {len(episodes) - max_on_first_run} older ones)"
        )
        episodes = episodes[:max_on_first_run]

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


_UMLAUT_MAP = str.maketrans("äöüÄÖÜß", "aoauouss")

def _build_transcript_url(date_str: str, title: str) -> str:
    """Build doppelgaenger.ai URL from date and episode title.

    The site uses slugs like: 2024-03-07_Episode_Title_Here
    Umlauts are transliterated and special chars replaced with underscores.
    """
    slug = title.translate(_UMLAUT_MAP)
    slug = slug.replace(" ", "_").replace("/", "_").replace("|", "_").replace("-", "_")
    slug = "".join(c for c in slug if c.isalnum() or c == "_")
    # Collapse multiple underscores and strip trailing ones
    import re
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
Erstelle eine strukturierte, deutschsprachige Zusammenfassung mit folgenden Abschnitten:

## 🎙️ Themen dieser Folge
- Stichpunktliste der besprochenen Themen (5–10 Punkte)

## 💡 Kernaussagen & Meinungen
- Die wichtigsten Meinungen und Einschätzungen der Hosts zu den Themen
- Wer hat was gesagt (wenn erkennbar: Pip / Glöck)

## 📊 Unternehmen & Produkte
- Genannte Unternehmen, Produkte oder Personen mit kurzer Einordnung

## ⚡ Das Wichtigste in 3 Sätzen
Eine sehr kurze Zusammenfassung für jemanden der nur 30 Sekunden Zeit hat.

Halte die Zusammenfassung prägnant und informativ. Fokus auf Fakten, Zahlen und konkrete Meinungen."""


def summarize_with_claude(transcript: str, episode_title: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    # Truncate transcript to ~100k chars to stay within context limits
    truncated = transcript[:100_000]
    if len(transcript) > 100_000:
        truncated += "\n\n[Transkript wurde auf 100.000 Zeichen gekürzt]"

    log.info(f"Summarizing episode '{episode_title}' with Claude …")
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
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


def build_email(episode: dict, summary: str, transcript_source: str) -> tuple[str, str, str]:
    """Return (subject, html, plain_text)."""
    title = episode["title"]
    published = episode.get("published", "")
    subject = f"🎙️ Doppelgänger Zusammenfassung: {title}"

    # Convert markdown-ish summary to simple HTML
    html_summary = summary.replace("\n", "<br>\n")
    for heading_marker in ["## 🎙️", "## 💡", "## 📊", "## ⚡"]:
        html_summary = html_summary.replace(
            heading_marker,
            f"<h3>{heading_marker.replace('## ', '')}"
        )
    # Close h3 tags
    import re
    html_summary = re.sub(r"(<h3>[^<]+)", r"\1</h3>", html_summary)

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 700px; margin: 0 auto; padding: 20px; color: #333; }}
    h1 {{ color: #1a1a2e; border-bottom: 2px solid #e94560; padding-bottom: 10px; }}
    h3 {{ color: #16213e; margin-top: 24px; }}
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
    Automatisch erstellt mit Claude AI &amp; dem Doppelgänger Podcast Summarizer<br>
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

    episodes = fetch_new_episodes(last_id)
    if not episodes:
        log.info("No new episodes. Nothing to do.")
        return

    for episode in episodes:
        log.info(f"Processing: {episode['title']}")
        transcript, source = get_transcript(episode)

        if not transcript:
            log.error(f"No transcript available for: {episode['title']}")
            continue

        summary = summarize_with_claude(transcript, episode["title"])
        subject, html, plain = build_email(episode, summary, source)
        send_email(subject, html, plain)

        save_state(episode["id"], episode["published"])
        log.info(f"Done: {episode['title']}")
        time.sleep(2)  # be polite between episodes

    log.info("All episodes processed.")


if __name__ == "__main__":
    main()
