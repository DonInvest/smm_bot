# SMM Bot — Telegram to X & Farcaster

A Telegram bot that translates your posts to English and publishes them to **X (Twitter)** and **Farcaster** in one click. Built for content creators and crypto-native workflows.

## Features

- **Telegram → X + Farcaster**: Forward a post (text or photo with caption); get an English translation and post to both platforms.
- **Smart limits**: Respects X (280 chars) and Farcaster (320 bytes); offers 3 shortening variants when the translation doesn’t fit.
- **Link preservation**: Extracts Telegram entities (links, bold, spoilers) so URLs and structure are kept in the translation.
- **X token refresh**: Automatically refreshes X OAuth2 access token on 401 so you don’t have to re-authorize in the browser.
- **Optional image**: If you send a photo with the post, it’s uploaded to X with the tweet.

## Stack

- **Python 3.9+**
- **Telegram**: `python-telegram-bot`
- **Translation**: Google Gemini API (e.g. `gemini-2.5-flash`)
- **X**: OAuth 2.0 (PKCE) + optional OAuth 1.0a for media upload
- **Farcaster**: Neynar API (managed signer)

## Setup

1. **Clone and install**
   ```bash
   git clone https://github.com/YOUR_USERNAME/smm_bot.git
   cd smm_bot
   pip install -r requirements.txt
   ```

2. **Environment**  
   Copy `.env.example` to `.env` and fill in:
   - `GEMINI_API_KEY` — [Google AI Studio](https://ai.google.dev)
   - `TELEGRAM_BOT_TOKEN` — [@BotFather](https://t.me/BotFather)
   - **X**: run `python auth_x.py` once to get `X_USER_ACCESS_TOKEN` and `X_REFRESH_TOKEN`; add `X_CLIENT_ID`, `X_CLIENT_SECRET` (from [X Developer Portal](https://developer.x.com)).
   - **Farcaster**: `NEYNAR_API_KEY`, `NEYNAR_SIGNER_UUID` (create signer via `python auth_farcaster.py` and approve in Warpcast).

3. **Run**
   ```bash
   python main.py
   ```

See inline comments in `main.py`, `auth_x.py`, and `auth_farcaster.py` for details.

## Project structure

```
smm_bot/
├── main.py              # Bot logic, translation, X + Farcaster posting
├── auth_x.py            # One-time OAuth2 PKCE flow for X tokens
├── auth_farcaster.py    # Create Neynar signer for Farcaster
├── requirements.txt
├── .env                 # Your keys (not committed)
└── README.md
```

## License

MIT
