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
        "places.photos",
    ]
)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PHOTO_WIDTH = 800

# How people actually get to dinner. Each mode has its own pace, its own sensible set of
# travel times to offer, and therefore its own search radius — half an hour of driving
# reaches a different city than half an hour of walking.
# Streets don't run in straight lines. Real paths through a grid are reliably about a
# third longer than the crow-flies distance we measure, so scale before estimating time —
# without this every mode reads optimistic, and the longer the trip the worse it gets.
CIRCUITY = 1.3

# `overhead_minutes` is the fixed cost before you cover any ground — waiting for a bus,
# walking to the stop. Without it, short transit trips read as far quicker than they are.
# Speeds are fitted against real Routes results rather than guessed, because an optimistic
# estimate would have the quiz offer a "20 min ride" for a trip that takes an hour — the
# exact failure this project exists to avoid.
MODES = {
    "WALK": {
        "verb": "walk",
        "metres_per_minute": 70,
        "overhead_minutes": 0,
        "ladder": [5, 10, 15, 20, 30],
    },
    "TRANSIT": {
        "verb": "ride",
        "metres_per_minute": 160,
        "overhead_minutes": 8,
        "ladder": [10, 20, 30, 45],
    },
    "DRIVE": {
        "verb": "drive",
        # City driving, not motorway — parking and lights eat the difference.
        "metres_per_minute": 650,
        "overhead_minutes": 0,
        "ladder": [10, 15, 20, 30],
    },
}
DEFAULT_MODE = "WALK"

# Places caps the search radius, and a huge one returns landmarks rather than dinner.
MAX_RADIUS_M = 50000


def mode_config(mode):
    return MODES.get(mode, MODES[DEFAULT_MODE])


def travel_minutes(metres, mode):
    """Estimated door-to-door time for a straight-line distance, in the given mode."""
    cfg = mode_config(mode)
    travelled = metres * CIRCUITY
    return max(1, round(cfg["overhead_minutes"] + travelled / cfg["metres_per_minute"]))


def search_radius(mode):
    """How far out to search: the crow-flies radius the widest question can still reach."""
    cfg = mode_config(mode)
    moving = max(cfg["ladder"]) - cfg["overhead_minutes"]
    reachable = moving * cfg["metres_per_minute"] / CIRCUITY
    return min(MAX_RADIUS_M, max(round(reachable), 1000))


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


def _search(lat, lon, included_types, key, radius):
    body = {
        "includedTypes": included_types,
        "maxResultCount": 20,  # hard ceiling in the Places API
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": radius}
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


def survey(lat, lon, mode=DEFAULT_MODE):
    """Return nearby eateries, nearest first, annotated with travel time for `mode`.

    Two searches because the API caps each at 20 results and cafes would otherwise be
    crowded out by restaurants entirely. The radius follows the mode, so choosing to
    drive genuinely widens the net rather than just relabelling the same walkable spots.
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise MapsError("GOOGLE_MAPS_API_KEY is not set")

    radius = search_radius(mode)
    raw = _search(lat, lon, ["restaurant"], key, radius) + _search(lat, lon, ["cafe"], key, radius)

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
            # Straight-line estimate for the chosen mode. The selected place gets a real
            # routed time later; routing all of these up front would be one call each.
            "minutes": travel_minutes(metres, mode),
            "price": PRICE_LABELS.get(p.get("priceLevel")),
            "rating": p.get("rating"),
            "reviews": p.get("userRatingCount") or 0,
            "kind": p.get("primaryTypeDisplayName", {}).get("text"),
            "url": p.get("googleMapsUri"),
            "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
            "summary": (p.get("editorialSummary") or {}).get("text"),
            # Just the resource name here — resolving it to an image costs a call, so
            # that waits until we know which places actually get shown.
            "photo_ref": next((ph["name"] for ph in (p.get("photos") or []) if ph.get("name")), None),
        }

    return sorted(places.values(), key=lambda p: p["metres"])


def photo_url(photo_ref):
    """Resolve a photo resource name to a directly-loadable image URL.

    `skipHttpRedirect` hands back the googleusercontent link as JSON instead of a 302,
    so the browser can load the image without ever seeing our API key.
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key or not photo_ref:
        return None
    try:
        resp = requests.get(
            f"https://places.googleapis.com/v1/{photo_ref}/media",
            params={"maxWidthPx": PHOTO_WIDTH, "skipHttpRedirect": "true", "key": key},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("photoUri")
    except requests.RequestException:
        return None


def _compute_route(from_lat, from_lon, to_lat, to_lon, mode, key, timeout=15):
    """Raw Routes call. Returns the parsed first route, or None if there isn't one."""
    body = {
        "origin": {"location": {"latLng": {"latitude": from_lat, "longitude": from_lon}}},
        "destination": {"location": {"latLng": {"latitude": to_lat, "longitude": to_lon}}},
        "travelMode": mode if mode in MODES else DEFAULT_MODE,
    }
    resp = requests.post(
        ROUTES_URL,
        json=body,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise MapsError(f"Routes API {resp.status_code}: {resp.text[:200]}")

    routes = resp.json().get("routes") or []
    if not routes:
        return None
    route = routes[0]
    seconds = int(str(route.get("duration", "0s")).rstrip("s") or 0)
    return {
        "polyline": (route.get("polyline") or {}).get("encodedPolyline"),
        "metres": route.get("distanceMeters"),
        "minutes": max(1, round(seconds / 60)),
    }


def travel_route(from_lat, from_lon, to_lat, to_lon, mode=DEFAULT_MODE):
    """The real path between two points for the chosen mode, as an encoded polyline."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise MapsError("GOOGLE_MAPS_API_KEY is not set")
    return _compute_route(from_lat, from_lon, to_lat, to_lon, mode, key)


# Far enough that any real transit network would be used to cover it, close enough that
# somewhere served by buses will still return a route.
TRANSIT_PROBE_M = 4000


def transit_available(lat, lon):
    """Does public transit actually serve this area?

    Asks the Routes API for a transit trip to a point a few km away. No route back means
    no usable network here, so the quiz shouldn't offer transit as a way to get to dinner.
    Any failure is treated as "no" — offering a mode that can't route is worse than
    omitting one that could have worked.
    """
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        return False
    # ~4 km north; one degree of latitude is roughly 111 km everywhere.
    probe_lat = lat + (TRANSIT_PROBE_M / 111000)
    try:
        return _compute_route(lat, lon, probe_lat, lon, "TRANSIT", key, timeout=12) is not None
    except (MapsError, requests.RequestException):
        return False
