"""Google Maps Platform lookups: find the real candidate restaurants around a point.

Everything the quiz asks about is derived from this list, so the questions can never
offer a filter that has no results behind it.
"""

import math
import os

import requests

PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Field mask is mandatory on Places (New) and is what you get billed on — ask only
# for what the quiz actually filters or renders with.
FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.location",
        "places.priceLevel",
        "places.rating",
        "places.userRatingCount",
        "places.primaryTypeDisplayName",
        "places.googleMapsUri",
        "places.currentOpeningHours.openNow",
        "places.editorialSummary",
    ]
)

# Comfortable walking pace. Used to turn metres into the "X minute walk" the quiz asks about.
METRES_PER_MINUTE = 80

# The widest question the quiz will ever ask is a 30 minute walk.
MAX_WALK_MINUTES = 30
SEARCH_RADIUS_M = MAX_WALK_MINUTES * METRES_PER_MINUTE

PRICE_LABELS = {
    "PRICE_LEVEL_FREE": "$",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


class MapsError(RuntimeError):
    """Places API refused the request — surfaced to the UI rather than a 500 page."""


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _search(lat, lon, included_types, key):
    body = {
        "includedTypes": included_types,
        "maxResultCount": 20,  # hard ceiling in the Places API
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": SEARCH_RADIUS_M}
        },
    }
    resp = requests.post(
        PLACES_URL,
        json=body,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS},
        timeout=15,
    )
    if resp.status_code != 200:
        raise MapsError(f"Places API {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("places", [])


def survey(lat, lon):
    """Return nearby eateries, nearest first, annotated with walk time.

    Two searches because the API caps each at 20 results and cafes would otherwise be
    crowded out by restaurants entirely.
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise MapsError("GOOGLE_MAPS_API_KEY is not set")

    raw = _search(lat, lon, ["restaurant"], key) + _search(lat, lon, ["cafe"], key)

    places = {}
    for p in raw:
        pid = p.get("id")
        loc = p.get("location") or {}
        if not pid or pid in places or "latitude" not in loc:
            continue
        metres = haversine_m(lat, lon, loc["latitude"], loc["longitude"])
        places[pid] = {
            "id": pid,
            "name": (p.get("displayName") or {}).get("text", "Unknown"),
            # The survey already knows where each place is, so the map can drop a marker
            # without the browser re-fetching every location through the Places library.
            "lat": loc["latitude"],
            "lng": loc["longitude"],
            "metres": round(metres),
            "walk": max(1, round(metres / METRES_PER_MINUTE)),
            "price": PRICE_LABELS.get(p.get("priceLevel")),
            "rating": p.get("rating"),
            "reviews": p.get("userRatingCount") or 0,
            "kind": p.get("primaryTypeDisplayName", {}).get("text"),
            "url": p.get("googleMapsUri"),
            "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
            "summary": (p.get("editorialSummary") or {}).get("text"),
        }

    return sorted(places.values(), key=lambda p: p["metres"])
