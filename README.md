# Telegram Tweet & Instagram Content Extractor Bot

A Telegram bot that extracts and translates tweet text from Twitter/X links and downloads media from Instagram posts/reels shared in chats.

## Features

- **Automatic link detection**: Twitter/X, Instagram, and YouTube links in messages (including links hidden behind text, and share formats)
- **YouTube support**: Prompts with an inline keyboard to choose the preferred format (high quality MP4, low quality MP4, or audio MP3/M4A), downloads strictly under Telegram's 50MB limit, sends natively, and leaves zero server residue
- **Tweet extraction & translation**: Extracts tweet text and translates non-Persian tweets to Persian (target language configurable)
- **X media support**: Sends tweet photos and videos
- **Instagram support**: Downloads and sends images/videos from public posts and reels, including carousels (in the correct order)
- **Albums**: Multiple media are sent as Telegram albums; large sets are split into evenly sized albums
- **Large media fallback**: If Telegram can't fetch a media URL itself (size limits, blocked host), the bot downloads it and uploads it directly (up to 50 MB)
- **Multi-chat support**: Private chats, groups, supergroups — including forum topics
- **Progress feedback**: Status message while working (best-effort; a failed status edit never aborts a job)
- **Robust error handling**: A failing update is logged and skipped; it can no longer block the bot

## Prerequisites

- Python 3.9+
- A Telegram Bot Token (from [@BotFather](https://t.me/botfather))

## Installation

```
git clone <repo>
cd <repo>

python -m venv venv
```

Activate:

- Windows: `venv\Scripts\activate`
- macOS/Linux: `source venv/bin/activate`

Install:

```
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill it in:

| Variable | Default | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | – | **Required.** Token from BotFather |
| `POLL_INTERVAL` | `1` | Seconds to wait after an empty poll |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, ... |
| `TRANSLATE_TO` | `fa` | Translation language code; empty disables translation |
| `PROCESS_EDITED_MESSAGES` | `0` | `1` to reply again when a message is edited |
| `INSTAGRAM_USERNAME` | – | Username of a saved Instaloader session (see below) |
| `INSTAGRAM_SESSION_FILE` | – | Optional path to that session file |
| `OFFSET_FILE` | `offset.txt` | Where the last processed update id is stored |

### Instagram on a server

Instagram frequently blocks anonymous requests from datacenter IPs. Log in once
and let the bot reuse the session:

```
instaloader --login YOUR_USERNAME
```

then set `INSTAGRAM_USERNAME=YOUR_USERNAME` in `.env`. Use a throwaway account,
not your main one.

## Usage

```
python main.py
```

Stop with Ctrl+C, or `SIGTERM` (systemd / Docker) for a graceful shutdown.

## Tests

```
python -m unittest discover -s tests -t .
```

No network, token, or `instaloader` needed.

## Project layout

| File | Purpose |
| --- | --- |
| `main.py` | Polling loop, message routing, sending logic |
| `telegram_api.py` | Bot API wrapper (single `_call` with error handling, rate-limit retry, token-safe errors) |
| `x_client.py` | Fetches tweets via FxTwitter |
| `instagram_client.py` | Downloads Instagram posts into a private temp dir |
| `downloader.py` | Downloads remote media for the upload fallback |
| `helpers.py` | Link regexes, text/album chunking, reply text, offset file |
| `models.py` | `MediaItem` (photo/video by URL or local path) |
| `constants.py` | Settings |

## How it works

### Twitter/X

1. Detects X links, extracts the tweet ID
2. Fetches tweet content via fxtwitter, translated if needed
3. Sends text as the caption when it fits in 1024 characters; otherwise the text goes first as normal message(s) and the media follows

### Instagram

1. Detects post/reel links
2. Downloads the media with Instaloader into a unique temp folder (always removed afterwards)
3. Sends a single media item or the carousel as album(s), with the caption handled like above

## Limitations

- **Instagram private content**: Only public posts and reels are supported without a logged-in session.
- **Instagram stability**: Relies on unofficial methods (Instaloader) and may break if Instagram changes its structure.
- **Telegram album limits**: Albums hold 2–10 items; larger sets are split evenly across several albums.
- **Caption length**: Captions are limited to 1024 characters; longer text is sent as separate messages.
- **File size**: Telegram bots can upload at most 50 MB per file.
- **Rate limits**: Heavy usage may hit Telegram, Instagram or X limits. Telegram flood-control (HTTP 429) is retried once automatically.
- **X API dependency**: Tweet data comes from a third-party service (fxtwitter), which may occasionally fail.
- **Single-threaded**: Links are processed one at a time, so a slow Instagram download delays other chats. At most 5 links are processed per message.
