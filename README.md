# Mood Food Finder

A locally-hosted web app that picks restaurants for you based on your **mood** — no typing,
just tap buttons. Powered by Gemini with the Google Maps grounding tool.

## Setup

1. Put your Gemini API key in `.env`:
   ```
   GEMINI_API_KEY=your-key-here
   ```
2. Install deps (uses [uv](https://docs.astral.sh/uv/)):
   ```
   uv sync
   ```

## Run

```
uv run app.py
```

Then open http://127.0.0.1:5000 in your browser.

Tap a mood, walk distance, budget, and food type, then hit **Find my spot**. The app uses your
browser's geolocation (falls back to a default area if you decline).
