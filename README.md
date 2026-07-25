# Mood Food Finder

A locally-hosted web app that picks restaurants for you — no typing, just tap buttons.
It surveys the real places around you with the Google Maps Platform, lets Gemini decide
which question to ask next, and shows the results on a live map.

## How it works

1. **Survey first.** On start, the server searches Places API (New) around your location
   and works out each spot's walk time from its coordinates.
2. **Ask only what's answerable.** Options are computed from that candidate list, so a
   filter never appears unless real places sit behind it — in a sparse area the walk
   question is skipped rather than offering a "5 min" that returns nothing.
3. **Gemini picks the next question** from the filters that still narrow things down, and
   writes the tile copy. Distance always leads.
4. **Results on a map.** Survivors are ranked with a reason each, plotted as markers, and
   listed in a bottom sheet you can tap through.

## Setup

1. Put your Gemini API key in `.env`:
   ```
   GEMINI_API_KEY=your-key-here
   ```
2. Put your Google Maps Platform key in `.env`:
   ```
   GOOGLE_MAPS_API_KEY=your-key-here
   ```
   It needs the Maps JavaScript API, Places API (New), and Geocoding API enabled on the
   project, with billing turned on.
3. Install deps (uses [uv](https://docs.astral.sh/uv/)):
   ```
   uv sync
   ```

## Run

```
uv run app.py
```

Then open http://127.0.0.1:5001 in your browser.

The app uses your browser's geolocation and falls back to a default area if you decline.
