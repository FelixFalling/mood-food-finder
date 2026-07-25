# Mood Food Finder

Picks somewhere to eat without making you type anything. You tap a few tiles, it narrows
down real restaurants around you, and shows the winner on a map with the walking route.

<p align="center">
  <img src="docs/demo.gif" width="380" alt="Tapping through the quiz: questions narrow the field, then the pick appears on a map with the walking route and a photo" />
</p>

The point of the project is that **the quiz is built from reality, not hard-coded**. Before
it asks anything it looks up what's actually nearby, then only offers options that have
places behind them. If nothing is a 5-minute walk away, it never offers "5 min".

<p align="center">
  <img src="docs/screenshots/01-question.png" width="30%" alt="Distance question with tiles generated from nearby places" />
  <img src="docs/screenshots/02-question.png" width="30%" alt="Budget question, narrowed to the price levels that exist" />
  <img src="docs/screenshots/03-results.png" width="30%" alt="Result on a map with the walking route and a photo" />
</p>

## How it works

```
browser ──▶ /api/start ──▶ Places API (New)  ──▶ candidate list (37 places, walk times)
                                │
        ◀── question ───────────┤ Gemini picks the next filter + writes the tile copy
                                │
browser ──▶ /api/answer ────────┤ deterministic filtering in Python
                                │
        ◀── results ────────────┴ Gemini ranks survivors, Places photo, Routes API path
```

**1. Survey first.** `/api/start` searches Places API (New) around your location — twice, for
`restaurant` and `cafe`, because the API caps each search at 20 results. Each place gets a
walk-time estimate from its coordinates at 80 m/min.

**2. Compute what's answerable.** `quiz.py` turns that candidate list into the filters that
still *discriminate*: distance thresholds, price levels, cuisines, open-now, well-rated. An
option is dropped if nothing sits behind it, and a whole question is dropped if it has fewer
than two live options. This is what makes dead-end taps impossible.

**3. Let Gemini choose the question.** The model receives only that validated menu and picks
which filter to ask about next, writing the title, labels and emoji. Its labels are matched
back to the server's options positionally, so the wording is free but the *filter* can't be
invented. Distance is always forced first — it cuts the field hardest.

**4. Narrow deterministically.** Filtering happens in Python, not in the model. Questions stop
once four or fewer candidates remain, or after four questions.

**5. Show the answer.** Gemini ranks the survivors with a one-line reason each, grounded in
real fields. The results screen plots them on a Maps JavaScript map, marks where you started,
draws the real walking path from the Routes API, and shows a Places photo.

## Google Maps Platform usage

| API | Used for |
|---|---|
| Places API (New) — `searchNearby` | The candidate survey that every question is derived from |
| Places API (New) — photo media | The image on the result card |
| Routes API | The walking path and its true duration |
| Maps JavaScript API | The map, markers, and polyline |

Two cost guards, since these are billed per call:

- **Route legs are cached per session**, so tapping between pins doesn't re-bill. A repeat
  lookup returns in ~2 ms.
- **Photos are resolved only for the places shown** (top 6), in parallel.

Photo URLs are fetched server-side with `skipHttpRedirect=true`, which returns the
`googleusercontent.com` link as JSON. The image loads in the browser without the API key ever
appearing in a URL.

### A note on walk times

The quiz filters on a straight-line estimate, because routing all ~37 candidates up front
would mean 37 billed calls per quiz. The selected place then gets a real routed time, which is
usually longer — downtown LA showed **22 min estimated vs 27 min actual**, since you can't walk
diagonally through buildings. The card shows the routed number once it arrives.

## Project layout

| File | Role |
|---|---|
| `app.py` | Flask routes, session state, and the two Gemini calls |
| `maps.py` | Places survey, photo resolution, Routes lookups, haversine distance |
| `quiz.py` | Works out which questions still discriminate — no network, no LLM |
| `templates/index.html` | The whole frontend: tiles, map, markers, route, bottom sheet |

## Setup

1. Put your Gemini API key in `.env`:
   ```
   GEMINI_API_KEY=your-key-here
   ```
2. Put your Google Maps Platform key in `.env`:
   ```
   GOOGLE_MAPS_API_KEY=your-key-here
   ```
   The project needs **Maps JavaScript API**, **Places API (New)**, and **Routes API** enabled,
   with billing on. Restrict the key to those services — and add an HTTP-referrer restriction
   before deploying anywhere, since the Maps JS key is necessarily visible in the browser.
3. Install deps (uses [uv](https://docs.astral.sh/uv/)):
   ```
   uv sync
   ```

## Run

```
uv run app.py
```

Open http://127.0.0.1:5001. The app asks for geolocation and falls back to downtown LA if you
decline.

## Limitations

- **Sessions live in an in-memory dict**, so this is single-process only. Running multiple
  workers would need Redis or signed client-side state.
- **The survey is capped at 40 places** (two 20-result searches), so in a dense city the quiz
  narrows a sample rather than every option.
- Places sometimes returns non-restaurants that are tagged as serving food — a grocery with a
  deli counter can surface as a result.
