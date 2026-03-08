# Doppelgänger Podcast Digest

Automatically summarizes new [Doppelgänger Tech Talk](https://www.doppelgaenger.io) episodes and delivers a structured digest to your inbox — twice a week, fully automated via GitHub Actions.

## What it does

```
RSS Feed
   ↓
Detect new episodes (max 2 per run)
   ↓
Fetch transcript from doppelgaenger.ai
   ↳ Fallback: OpenAI Whisper (audio transcription)
   ↳ Fallback: RSS description
   ↓
Summarize with Claude AI (claude-sonnet-4-6)
   ↓
Send HTML email digest
```

## What the email looks like

Each digest is structured around the podcast's recurring segments:

- **⚡ Das Wichtigste** — 6-sentence summary for a 2-minute read
- **📊 Earnings** — Quarterly results with host opinions *(if covered)*
- **🚨 Schmuddelecke** — Controversial/shady tech topics *(if covered)*
- **🎙️ Weitere Themen** — All other topics, each with facts + host opinions

## Setup (fork & run yourself)

### 1. Fork this repository

Click **Fork** in the top right on GitHub.

### 2. Add GitHub Secrets

Go to `Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | API key from [console.anthropic.com](https://console.anthropic.com) |
| `EMAIL_FROM` | Sender email address (e.g. your Gmail) |
| `EMAIL_TO` | Recipient email address |
| `EMAIL_PASSWORD` | App password — see below |
| `EMAIL_SMTP_HOST` | SMTP server (default: `smtp.gmail.com`) |
| `EMAIL_SMTP_PORT` | SMTP port (default: `587`) |
| `OPENAI_API_KEY` | *(Optional)* Only needed if doppelgaenger.ai is unavailable |

### 3. Create a Gmail App Password

1. Enable 2-factor authentication on your Google account
2. Go to **Google Account → Security → App Passwords**
3. Create a new app password for "Mail"
4. Use the 16-character code as `EMAIL_PASSWORD`

Other email providers work too — just set `EMAIL_SMTP_HOST` and `EMAIL_SMTP_PORT` accordingly.

### 4. Enable GitHub Actions

The workflow runs automatically:
- **Wednesday** at 10:00 UTC
- **Saturday** at 10:00 UTC

You can also trigger it manually via `Actions → Doppelgänger Podcast Digest → Run workflow`.

## Run locally

```bash
git clone https://github.com/YOUR_USERNAME/Doppelganger
cd Doppelganger
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export EMAIL_FROM="you@gmail.com"
export EMAIL_TO="you@gmail.com"
export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"

python summarizer.py
```

## Costs

Per episode (transcript ~20k tokens input, summary ~800 tokens output):

| Model | Approx. cost |
|-------|-------------|
| claude-sonnet-4-6 *(default)* | ~$0.05 |
| claude-haiku-4-5 | ~$0.02 |

Whisper fallback (only if doppelgaenger.ai is down): ~$0.50–1.00/episode depending on length.

## Transcript sources

1. **doppelgaenger.ai** — Unofficial site with AI transcripts for all episodes (primary, free)
2. **OpenAI Whisper** — Audio transcription fallback (costs apply)
3. **RSS description** — Last resort fallback (short text only)

## Adapting for other podcasts

The summarizer is not hardcoded to Doppelgänger. To use it for another podcast:

1. Change `RSS_FEED` in `summarizer.py` to your podcast's RSS feed URL
2. Remove or update `TRANSCRIPT_BASE` (the doppelgaenger.ai scraping)
3. Adjust `SUMMARY_PROMPT` to match the format/language you want
4. Update the cron schedule in `.github/workflows/podcast_digest.yml`
