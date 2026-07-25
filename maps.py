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


def walk_route(from_lat, from_lon, to_lat, to_lon):
    """The actual walking path between two points, as an encoded polyline."""
    key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise MapsError("GOOGLE_MAPS_API_KEY is not set")

    body = {
        "origin": {"location": {"latLng": {"latitude": from_lat, "longitude": from_lon}}},
        "destination": {"location": {"latLng": {"latitude": to_lat, "longitude": to_lon}}},
        "travelMode": "WALK",
    }
    resp = requests.post(
        ROUTES_URL,
        json=body,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
        },
        timeout=15,
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
