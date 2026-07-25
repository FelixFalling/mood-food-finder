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

# The lite model answers both of our prompts — short copywriting and ranking a handful of
# candidates — as reliably as the full flash model, and measured 3-5x faster on each. Since
# every call sits directly between a tap and the next screen, that latency is the whole
# experience. Pinned rather than `-latest` so the quiz's behaviour doesn't drift underneath us.
MODEL = "gemini-3.5-flash-lite"

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
    # A position in the candidate list, not an ID. Opaque Places IDs get hallucinated
    # (one run in three), while a small integer doesn't — and it's far cheaper to emit.
    index: int
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


def choose_best(candidates, answers, mode):
    """Rank the survivors and say why, grounded in the real place data."""
    verb = maps.mode_config(mode)["verb"]
    menu = "\n".join(
        f"{i + 1}. {p['name']} | {p['kind'] or 'eatery'} | {p['price'] or 'price n/a'} | "
        f"rating {p['rating'] or 'n/a'} ({p['reviews']} reviews)"
        f"{' | ' + p['summary'] if p['summary'] else ''}"
        for i, p in enumerate(candidates)
    )
    prompt = (
        "Pick the best restaurants for someone whose quiz answers were: "
        f"{describe(answers)}.\n\nCandidates:\n{menu}\n\n"
        f"They are travelling by {verb}. Rank them best first, using each candidate's number "
        "as `index`, and include every candidate exactly once. For each, give one warm "
        "sentence on why it fits their answers, referencing concrete details (price, rating, "
        "cuisine). Do NOT state a travel time or distance — the card shows the real routed "
        "time and your estimate would contradict it."
    )

    reply = ask_model(prompt, Picks)
    remaining = list(enumerate(candidates))
    ranked = []
    if reply:
        taken = set()
        for pick in reply.picks:
            i = pick.index - 1  # the prompt numbers them from 1
            if 0 <= i < len(candidates) and i not in taken:
                taken.add(i)
                ranked.append({**candidates[i], "reason": pick.reason})
        remaining = [(i, p) for i, p in remaining if i not in taken]
    # Anything the model dropped still beats showing the user nothing.
    ranked += [{**p, "reason": p["summary"] or ""} for _, p in remaining]
    return ranked


# Resolving a photo is one call each, so only the places the user can actually see get one.
PHOTOS_SHOWN = 6


def dress_results(ranked, session):
    """Attach photos, and the top pick's route, before the results screen is sent.

    All of it runs concurrently: the photo lookups are independent of each other and of the
    route, so the whole step costs about as long as its slowest single call. Shipping the
    route with the results also means the map draws its path on first paint instead of
    waiting for the browser to come back and ask for it.
    """
    head = ranked[:PHOTOS_SHOWN]
    origin = session["origin"]
    mode = session["mode"]

    def route_for_top():
        if not ranked:
            return None
        top = ranked[0]
        try:
            return maps.travel_route(origin["lat"], origin["lng"], top["lat"], top["lng"], mode)
        except maps.MapsError:
            return None  # the map still works, it just won't have a line yet

    with ThreadPoolExecutor(max_workers=PHOTOS_SHOWN + 1) as pool:
        pending_route = pool.submit(route_for_top)
        urls = list(pool.map(lambda p: maps.photo_url(p.get("photo_ref")), head))
        leg = pending_route.result()

    for place, url in zip(head, urls):
        place["photo"] = url
    if leg and ranked:
        ranked[0]["route"] = leg
        # Cache it under the same key /api/route uses, so re-selecting doesn't re-bill.
        session["routes"][ranked[0]["id"]] = leg
    return ranked


# Fixed copy, deliberately. Every other question is written by the model because *which*
# filter to ask about is a real judgement call, but the travel modes are always these three
# in this order — and this is the first screen, where latency is felt most. Writing it by
# hand takes the model off the critical path entirely.
MODE_TILES = {
    "WALK": {"label": "Walking", "emoji": "🚶", "sub": "on foot"},
    "TRANSIT": {"label": "Transit", "emoji": "🚌", "sub": "bus or train"},
    "DRIVE": {"label": "Driving", "emoji": "🚗", "sub": "by car"},
}


def mode_question(lat, lon):
    """How are you getting there? Asked before anything else, because the answer sets
    the search radius — half an hour of driving reaches far more than half an hour on foot.

    Transit is only offered where it actually runs; see maps.transit_available.
    """
    values = ["WALK", "TRANSIT", "DRIVE"] if maps.transit_available(lat, lon) else ["WALK", "DRIVE"]
    return {
        "key": "mode",
        "title": "How are you getting there?",
        "options": [{**MODE_TILES[v], "value": v} for v in values],
    }


def next_stage(session):
    """Either the next question, or the final results when narrowing is done."""
    candidates = session["candidates"]
    mode = session["mode"]
    available = quiz.dimensions(candidates, session["asked"], mode)

    # Distance leads, right after the travel mode that gives it meaning. It's the filter
    # that cuts the field hardest and the one the user can answer without thinking, so the
    # model doesn't get to pick something else first. In a sparse area `distance` won't be
    # offered at all, and then anything else will do.
    if session["asked"] == ["mode"] and "distance" in available:
        available = {"distance": available["distance"]}

    done = (
        len(candidates) <= ENOUGH_CANDIDATES
        or not available
        or len(session["asked"]) >= MAX_QUESTIONS
    )
    if done:
        ranked = choose_best(candidates, session["answers"], mode)
        return {
            "done": True,
            "places": dress_results(ranked, session),
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

    # No survey yet — how they're travelling decides how wide to search.
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {
        "candidates": [],
        "answers": {},
        "asked": [],
        "all": [],
        "mode": maps.DEFAULT_MODE,
        "origin": {"lat": lat, "lng": lon},
        "routes": {},
    }

    return jsonify(
        {
            "done": False,
            "question": mode_question(lat, lon),
            "asked": 0,
            "max_questions": MAX_QUESTIONS + 1,  # the mode question counts too
            "session": sid,
            "origin": SESSIONS[sid]["origin"],
        }
    )


@app.route("/api/answer", methods=["POST"])
def answer():
    data = request.get_json(force=True) or {}
    session = SESSIONS.get(data.get("session"))
    if not session:
        return jsonify({"error": "Session expired — start over."}), 404

    key, value = data.get("key"), data.get("value")

    # The travel mode isn't a filter — it decides how far afield to look, so the survey
    # only happens once we know it.
    if key == "mode":
        session["mode"] = value if value in maps.MODES else maps.DEFAULT_MODE
        try:
            candidates = maps.survey(
                session["origin"]["lat"], session["origin"]["lng"], session["mode"]
            )
        except maps.MapsError as exc:
            return jsonify({"error": str(exc)}), 502
        if not candidates:
            return jsonify({"error": "No restaurants found anywhere near you."}), 200
        session["candidates"] = candidates
        session["all"] = candidates
    else:
        narrowed = quiz.apply(session["candidates"], key, value)
        # Never let an answer empty the board; if it somehow would, keep the wider set.
        if narrowed:
            session["candidates"] = narrowed

    session["answers"][key] = value
    session["asked"].append(key)

    payload = next_stage(session)
    payload["found"] = len(session["all"])
    payload["mode"] = session["mode"]
    return jsonify(payload)


@app.route("/api/route", methods=["POST"])
def route():
    """The path from where the user started to one place, in their chosen travel mode."""
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
        leg = maps.travel_route(
            origin["lat"], origin["lng"], place["lat"], place["lng"], session["mode"]
        )
    except maps.MapsError as exc:
        return jsonify({"error": str(exc)}), 502

    session["routes"][place_id] = leg or {}
    return jsonify(leg or {})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
