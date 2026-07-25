"""Mood Food Finder — button-driven restaurant picker backed by Gemini + Google Maps."""

import os

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

    return jsonify({"text": "\n".join(text_chunks).strip(), "places": places})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
