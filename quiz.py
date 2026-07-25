"""Work out which questions are still worth asking about a set of candidate places.

The rule that drives everything here: an option is only offered if real candidates sit
behind it. That is why the quiz can't show "5 minute walk" in a neighbourhood where the
nearest kitchen is a 12 minute walk away.
"""

from collections import Counter

WALK_LADDER = [5, 10, 15, 20, 30]
PRICE_ORDER = ["$", "$$", "$$$", "$$$$"]
WELL_RATED = 4.3

# A cuisine has to show up this often to be worth a whole question tile.
MIN_PER_CUISINE = 2
MAX_CUISINES = 6


def matches(place, key, value):
    """Does one place survive this answer? `any` always passes."""
    if value == "any":
        return True
    if key == "walk":
        return place["walk"] <= int(value)
    if key == "price":
        return place["price"] == value
    if key == "cuisine":
        return place["kind"] == value
    if key == "open":
        return place["open_now"] is True
    if key == "rating":
        return (place["rating"] or 0) >= WELL_RATED
    return True


def apply(candidates, key, value):
    return [p for p in candidates if matches(p, key, value)]


def _walk_options(candidates):
    total = len(candidates)
    options = []
    seen_counts = set()
    for minutes in WALK_LADDER:
        count = sum(1 for p in candidates if p["walk"] <= minutes)
        # Skip a threshold that is empty, that covers everything (asks nothing), or that
        # captures exactly the same places as a tighter one already offered.
        if count == 0 or count == total or count in seen_counts:
            continue
        seen_counts.add(count)
        options.append({"value": str(minutes), "count": count, "hint": f"{minutes} min walk"})
    if options:
        options.append({"value": "any", "count": total, "hint": "any distance"})
    return options


def _tally_options(candidates, key, field, minimum=1, limit=None, order=None):
    counts = Counter(p[field] for p in candidates if p.get(field))
    items = [(v, c) for v, c in counts.items() if c >= minimum]
    if order:
        items.sort(key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    else:
        items.sort(key=lambda kv: -kv[1])
    if limit:
        items = items[:limit]
    return [{"value": v, "count": c, "hint": v} for v, c in items]


def _split_options(candidates, key, hint):
    """A yes/no filter, offered only when it actually splits the field."""
    kept = len(apply(candidates, key, "yes"))
    if kept == 0 or kept == len(candidates):
        return []
    return [
        {"value": "yes", "count": kept, "hint": hint},
        {"value": "any", "count": len(candidates), "hint": "doesn't matter"},
    ]


def dimensions(candidates, asked):
    """Every question still worth asking, with only the options that have results.

    A dimension needs at least two live options or it isn't a choice at all.
    """
    built = {
        "walk": _walk_options(candidates),
        "price": _tally_options(candidates, "price", "price", order=PRICE_ORDER),
        "cuisine": _tally_options(
            candidates, "cuisine", "kind", minimum=MIN_PER_CUISINE, limit=MAX_CUISINES
        ),
        "open": _split_options(candidates, "open", "open right now"),
        "rating": _split_options(candidates, "rating", f"rated {WELL_RATED}+"),
    }
    return {
        key: opts
        for key, opts in built.items()
        if key not in asked and len(opts) >= 2
    }
