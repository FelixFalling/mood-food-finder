"""Mood Food Finder — button-driven restaurant picker backed by Gemini + Google Maps."""

import os
import re

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from google import genai

load_dotenv()

app = Flask(__name__)
client = genai.Client()

# Default location if the browser won't share geolocation (downtown LA, from the docs example).
DEFAULT_LAT = 34.050481
DEFAULT_LON = -118.248526


def build_prompt(mood, walk, budget, food):
    """Turn the button selections into a natural-language request for Gemini."""
    parts = ["I'm looking for a restaurant near my current location."]

    if walk:
        parts.append(f"It should be within about a {walk}-minute walk.")
    if budget:
        parts.append(f"My budget is roughly {budget} (on a $ to $$$$ scale).")
    if food and food.lower() != "surprise me":
        parts.append(f"I'm in the mood for {food} food.")
    if mood:
        parts.append(f"Right now I'm feeling {mood.lower()}, so pick places that match that vibe.")

    parts.append(
        "Recommend 3 to 5 real, currently-open-ish options. For each, give the name, "
        "a one-sentence reason it fits, and the price level. Keep it warm and concise."
    )
    return " ".join(parts)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/recommend", methods=["POST"])
def recommend():
    data = request.get_json(force=True) or {}

    lat = data.get("latitude") or DEFAULT_LAT
    lon = data.get("longitude") or DEFAULT_LON
    prompt = build_prompt(
        mood=data.get("mood"),
        walk=data.get("walk"),
        budget=data.get("budget"),
        food=data.get("food"),
    )

    try:
        interaction = client.interactions.create(
            model="gemini-3.6-flash",
            input=prompt,
            tools=[{"type": "google_maps", "latitude": float(lat), "longitude": float(lon)}],
        )
    except Exception as exc:  # surface API/quota errors to the UI instead of a 500 page
        return jsonify({"error": str(exc)}), 502

    text_chunks = []
    places = []
    seen = set()
    for step in interaction.steps:
        if step.type != "model_output":
            continue
        for block in step.content:
            if block.type != "text":
                continue
            text_chunks.append(block.text)
            for annotation in (block.annotations or []):
                if annotation.type == "place_citation" and annotation.url not in seen:
                    seen.add(annotation.url)
                    places.append({"name": annotation.name, "url": annotation.url})

    text = "\n".join(text_chunks).strip()
    places = dedupe_places(places)
    attach_reasons(places, text)
    return jsonify({"text": text, "places": places})


def dedupe_places(places):
    """Collapse citations that are the same restaurant under different labels.

    Grounding sometimes returns both "Javier's" and "Javier's DTLA"; the UI shows one
    option at a time, so a repeat looks like a broken "show me another".
    """
    kept = []
    for place in places:
        key = search_key(place["name"])
        twin = next((k for k in kept if key in search_key(k["name"]) or search_key(k["name"]) in key), None)
        if twin is None:
            kept.append(place)
        elif len(place["name"]) > len(twin["name"]):
            twin.update(place)  # prefer the more specific label
    return kept


# A new list item starts at a top-level bullet or "1." marker, or after a blank line.
# The anchor must stay unindented: nested bullets ("   * Price Level: ...") are
# continuations of the entry above, not entries of their own.
ITEM_START = re.compile(r"^(?:[-*•]\s|#+\s|\d+[.)]\s)")
# Leading list marker on any line, indented or not.
BULLET = re.compile(r"^\s*(?:[-*•]\s*|#+\s*|\d+[.)]\s*)")
# A short "Price Level:" / "Why it fits:" style label the model puts before its prose.
LABEL = re.compile(r"^[A-Za-z][A-Za-z' ]{0,24}:\s*")


def search_key(text):
    """Lowercase and drop punctuation/emoji so citation names match the model's prose."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def clean_reason(text, name):
    """Flatten a recommendation chunk into one readable sentence-ish line.

    Strips markdown emphasis, list markers, the model's boilerplate field labels, and
    the place name itself (the card renders the name separately).
    """
    lines = []
    for line in text.splitlines():
        line = BULLET.sub("", line.replace("**", "").replace("__", "")).strip()
        line = re.sub(r"\*(\S[^*]*?)\*", r"\1", line)  # inline *emphasis*
        line = LABEL.sub("", line).strip()
        line = re.sub(r"^" + re.escape(name) + r"\s*[-–—:|]*\s*", "", line, flags=re.I).strip()
        line = line.strip("*").strip()
        if line:
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def split_items(text):
    """Break the model's prose into one chunk per recommendation.

    Gemini formats the list either as blank-line-separated paragraphs or as a run of
    bullet lines, so split on both boundaries.
    """
    items = []
    current = []
    for line in text.splitlines():
        if not line.strip():
            if current:
                items.append("\n".join(current))
                current = []
            continue
        if ITEM_START.match(line) and current:
            items.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        items.append("\n".join(current))
    return items


def attach_reasons(places, text):
    """Give each place the prose chunk that describes only it.

    The UI shows one place at a time, so it needs per-place copy rather than the
    single blob listing every option.
    """
    items = split_items(text)
    names = [search_key(p["name"]) for p in places]

    for place in places:
        name = search_key(place["name"])
        # Overlapping names ("Javier's" vs "Javier's DTLA") aren't rival mentions —
        # only a genuinely different place means the chunk covers more than this one.
        rivals = [o for o in names if o and o not in name and name not in o]

        def describes_only(item, name=name, rivals=rivals):
            body = search_key(item)
            return name in body and not any(o in body for o in rivals)

        # A chunk naming several places isn't about this one — skip it rather than
        # spoiling the other options on the card.
        match = next((item for item in items if describes_only(item)), None)
        place["reason"] = clean_reason(match, place["name"]) if match else ""


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
