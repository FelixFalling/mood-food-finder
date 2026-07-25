"""Mood Food Finder — an agentic, button-only quiz over restaurants that actually exist.

Flow: survey real places around the user first, then let Gemini choose the next question
from the filters that still discriminate. Because the option sets are computed from the
candidate list, the quiz can never offer a choice that leads to zero results.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from google import genai
from pydantic import BaseModel

import maps
import quiz

load_dotenv()

app = Flask(__name__)
client = genai.Client()

MODEL = "gemini-3.6-flash"

# Handed to the template so the Maps JavaScript API can draw the results map.
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# Default location if the browser won't share geolocation (downtown LA, from the docs example).
DEFAULT_LAT = 34.050481
DEFAULT_LON = -118.248526

# Stop asking once the field is this small — more questions would just be busywork.
ENOUGH_CANDIDATES = 4
MAX_QUESTIONS = 4

# Survey results per session. In-memory is fine for a single-process dev app; this is
# the one piece that would need real storage before running more than one worker.
SESSIONS = {}


class NextQuestion(BaseModel):
    dimension: str
    title: str
    labels: list[str]
    emojis: list[str]


class Pick(BaseModel):
    place_id: str
    reason: str


class Picks(BaseModel):
    picks: list[Pick]


def ask_model(prompt, schema):
    """One structured-output call. Returns the parsed object, or None if it misbehaves."""
    try:
        interaction = client.interactions.create(
            model=MODEL,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema.model_json_schema(),
            },
        )
        return schema.model_validate_json(interaction.output_text)
    except Exception:
        return None


def describe(answers):
    return ", ".join(f"{k}={v}" for k, v in answers.items()) or "nothing yet"


def compose_question(available, answers, remaining):
    """Have Gemini choose the next filter and write the tile copy.

    The model only ever names a dimension we offered, and its labels are matched back to
    our options positionally — so its wording can be creative without inventing a filter
    that has no places behind it.
    """
    menu = "\n".join(
        f"- {key}: options = {[o['hint'] for o in opts]} (each has "
        f"{[o['count'] for o in opts]} places)"
        for key, opts in available.items()
    )
    prompt = (
        "You are running a playful, button-only restaurant quiz. The user taps tiles; "
        "they never type.\n"
        f"So far they chose: {describe(answers)}.\n"
        f"{remaining} candidate places remain.\n\n"
        "Pick the ONE question that best narrows things down next, from this menu:\n"
        f"{menu}\n\n"
        "Return the dimension key you chose, a short punchy question title (max 6 words), "
        "and a label plus a single emoji for each option, in the SAME ORDER as the options "
        "listed for that dimension. Keep labels under 3 words and warm in tone."
    )

    reply = ask_model(prompt, NextQuestion)
    key = reply.dimension if reply and reply.dimension in available else next(iter(available))
    options = available[key]

    # Fall back to the plain hints whenever the model's copy doesn't line up one-to-one.
    if reply and reply.dimension == key and len(reply.labels) == len(options):
        labels = reply.labels
        emojis = reply.emojis if len(reply.emojis) == len(options) else [""] * len(options)
        title = reply.title
    else:
        labels = [o["hint"] for o in options]
        emojis = [""] * len(options)
        title = "What sounds right?"

    tiles = [
        {"label": labels[i], "emoji": emojis[i], "sub": o["hint"], "value": o["value"]}
        for i, o in enumerate(options)
    ]
    return {"key": key, "title": title, "options": tiles}


def choose_best(candidates, answers):
    """Rank the survivors and say why, grounded in the real place data."""
    menu = "\n".join(
        f"- id={p['id']} | {p['name']} | {p['kind'] or 'eatery'} | {p['price'] or 'price n/a'} | "
        f"{p['walk']} min walk | rating {p['rating'] or 'n/a'} ({p['reviews']} reviews)"
        f"{' | ' + p['summary'] if p['summary'] else ''}"
        for p in candidates
    )
    prompt = (
        "Pick the best restaurants for someone whose quiz answers were: "
        f"{describe(answers)}.\n\nCandidates:\n{menu}\n\n"
        "Rank them best first. For each, give its exact place_id and one warm sentence on "
        "why it fits their answers. Reference concrete details (walk time, price, rating, "
        "cuisine). Never invent a place that is not listed."
    )

    reply = ask_model(prompt, Picks)
    by_id = {p["id"]: p for p in candidates}
    ranked = []
    if reply:
        for pick in reply.picks:
            place = by_id.pop(pick.place_id, None)
            if place:
                ranked.append({**place, "reason": pick.reason})
    # Anything the model dropped still beats showing the user nothing.
    ranked += [{**p, "reason": p["summary"] or ""} for p in by_id.values()]
    return ranked


# Resolving a photo is one call each, so only the places the user can actually see get one.
PHOTOS_SHOWN = 6


def with_photos(ranked):
    """Attach a loadable image URL to the places the results screen will show."""
    head = ranked[:PHOTOS_SHOWN]
    with ThreadPoolExecutor(max_workers=6) as pool:
        urls = pool.map(lambda p: maps.photo_url(p.get("photo_ref")), head)
    for place, url in zip(head, urls):
        place["photo"] = url
    return ranked


def next_stage(session):
    """Either the next question, or the final results when narrowing is done."""
    candidates = session["candidates"]
    available = quiz.dimensions(candidates, session["asked"])

    # Distance leads. It's the filter that cuts the field hardest and the one the user
    # can answer without thinking, so the model doesn't get to pick something else first.
    # In a sparse area `walk` won't be offered at all, and then anything else will do.
    if not session["asked"] and "walk" in available:
        available = {"walk": available["walk"]}

    done = (
        len(candidates) <= ENOUGH_CANDIDATES
        or not available
        or len(session["asked"]) >= MAX_QUESTIONS
    )
    if done:
        ranked = choose_best(candidates, session["answers"])
        return {
            "done": True,
            "places": with_photos(ranked),
            "asked": len(session["asked"]),
        }

    question = compose_question(available, session["answers"], len(candidates))
    return {
        "done": False,
        "question": question,
        "remaining": len(candidates),
        "asked": len(session["asked"]),
        "max_questions": MAX_QUESTIONS,
    }


@app.route("/")
def index():
    return render_template("index.html", maps_api_key=GOOGLE_MAPS_API_KEY)


@app.route("/api/start", methods=["POST"])
def start():
    data = request.get_json(force=True) or {}
    lat = float(data.get("latitude") or DEFAULT_LAT)
    lon = float(data.get("longitude") or DEFAULT_LON)

    try:
        candidates = maps.survey(lat, lon)
    except maps.MapsError as exc:
        return jsonify({"error": str(exc)}), 502

    if not candidates:
        return jsonify({"error": "No restaurants found anywhere near you."}), 200

    sid = uuid.uuid4().hex
    SESSIONS[sid] = {
        "candidates": candidates,
        "answers": {},
        "asked": [],
        "all": candidates,
        "origin": {"lat": lat, "lng": lon},
        "routes": {},
    }

    payload = next_stage(SESSIONS[sid])
    payload["session"] = sid
    payload["found"] = len(candidates)
    payload["origin"] = SESSIONS[sid]["origin"]
    return jsonify(payload)


@app.route("/api/answer", methods=["POST"])
def answer():
    data = request.get_json(force=True) or {}
    session = SESSIONS.get(data.get("session"))
    if not session:
        return jsonify({"error": "Session expired — start over."}), 404

    key, value = data.get("key"), data.get("value")
    narrowed = quiz.apply(session["candidates"], key, value)

    # Never let an answer empty the board; if it somehow would, keep the wider set.
    if narrowed:
        session["candidates"] = narrowed
    session["answers"][key] = value
    session["asked"].append(key)

    return jsonify(next_stage(session))


@app.route("/api/route", methods=["POST"])
def route():
    """The walking path from where the user started to one place."""
    data = request.get_json(force=True) or {}
    session = SESSIONS.get(data.get("session"))
    if not session:
        return jsonify({"error": "Session expired — start over."}), 404

    place_id = data.get("place_id")
    # Each leg is a billed Routes call, so don't pay twice when the user taps back and forth.
    if place_id in session["routes"]:
        return jsonify(session["routes"][place_id])

    place = next((p for p in session["all"] if p["id"] == place_id), None)
    if not place:
        return jsonify({"error": "Unknown place."}), 404

    origin = session["origin"]
    try:
        leg = maps.walk_route(origin["lat"], origin["lng"], place["lat"], place["lng"])
    except maps.MapsError as exc:
        return jsonify({"error": str(exc)}), 502

    session["routes"][place_id] = leg or {}
    return jsonify(leg or {})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
