"""Landed cost in NOK, Pickup trips, Penalties and the daily exchange rate."""
import datetime
import math
import threading

from .config import (FINN_BUYER_FEE, FINN_SHIPPING_NOK, HOME_LAT_LON, PENALTY_NOK, PICKUP_MAX_MINUTES,
                     PICKUP_NOK_PER_KM, RISK, ROUTE_FALLBACK, VAT)
from .sources import REBUILDIT, http_json


class OsrmRouter:
    """Road km and minutes from home, via the public OSRM router, cached per location in the store."""

    def __init__(self, store, fetch=http_json):
        self._store, self._fetch = store, fetch

    def __call__(self, lat, lon):
        key = (round(lat, 3), round(lon, 3))
        cached = self._store.route_get(*key)
        if cached:
            return cached
        home_lat, home_lon = HOME_LAT_LON
        try:
            r = self._fetch(f"https://router.project-osrm.org/route/v1/driving/"
                            f"{home_lon},{home_lat};{key[1]},{key[0]}?overview=false")["routes"][0]
            km, minutes = r["distance"] / 1000, r["duration"] / 60
        except Exception:  # any router failure (network, HTTP, bad JSON) falls back; it must not end the Source
            # ponytail: straight line x factor at a fixed speed, not cached so the next Hunt retries OSRM
            factor, kmh = ROUTE_FALLBACK
            km = _great_circle_km(home_lat, home_lon, *key) * factor
            return round(km, 1), round(km / kmh * 60, 1)
        self._store.route_put(*key, round(km, 1), round(minutes, 1))
        return round(km, 1), round(minutes, 1)


def _great_circle_km(lat1, lon1, lat2, lon2):
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    return 6371 * math.acos(min(1, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)))


def machine_penalties(facts):
    """Penalty NOK per weakness, keyed so the page can tell a stated weakness from an unknown one.

    An unknown fact is charged like a missing one, so omitting it never pays. The caddy Penalty counts every
    empty 3.5" bay; the Build optimizer (#6) recounts it for a Build as max(0, disks + 1 boot - caddies) x 100.
    """
    missing_caddies = max(0, facts["bays_35"] - (facts["caddies_35"] or 0))
    penalties = {
        ("single_psu" if facts["psu_count"] == 1 else "psu_unknown"):
            PENALTY_NOK["single_psu"] if (facts["psu_count"] or 1) < 2 else 0,
        ("caddies" if facts["caddies_35"] is not None else "caddies_unknown"): PENALTY_NOK["caddy"] * missing_caddies,
        ("raid_only" if facts["controller"] == "raid" else "controller_unknown"):
            PENALTY_NOK["raid_only"] if facts["controller"] != "hba" else 0,
        ("no_rails" if facts["rails"] is False else "rails_unknown"):
            PENALTY_NOK["no_rails"] if facts["rails"] is not True else 0,
    }
    return {k: v for k, v in penalties.items() if v}


def cost_breakdown(listing, fx, foreign, router, kind, penalties=None):
    """(breakdown, problem). breakdown holds each NOK part and `total`; problem is None, "shipping" (unknown
    for a foreign Source), "location" (pickup-only without a place) or "too_far" (over the pickup limit).
    `kind` picks the finn.no shipping estimate; a finn.no Listing that ships pays the Trygg betaling fee, except one
    from Rebuild IT, which is bought in its own web shop."""
    parts = {"price": round(listing.price * fx(listing.currency), 2)}
    estimate, fee = FINN_SHIPPING_NOK[kind], round(FINN_BUYER_FEE[0] + FINN_BUYER_FEE[1] * parts["price"], 2)
    if listing.seller == REBUILDIT:
        fee = 0
    problem, trip = None, None
    if not foreign and listing.lat is not None and listing.lon is not None:
        km, minutes = router(listing.lat, listing.lon)
        parts["pickup_km"], parts["pickup_minutes"] = km, minutes
        if minutes <= PICKUP_MAX_MINUTES:
            trip = round(2 * km * PICKUP_NOK_PER_KM, 2)
    if listing.shipping is not None:
        parts["shipping"] = round(listing.shipping * fx(listing.shipping_currency or listing.currency), 2)
        if not foreign:
            parts["finn_fee"] = fee
    elif foreign:
        problem = "shipping"
    elif listing.pickup_only:
        if "pickup_minutes" not in parts:
            problem = "location"
        elif trip is None:
            problem = "too_far"
        else:
            parts["pickup_trip"] = trip
    elif trip is not None and trip < estimate + fee:  # finn with Fiks ferdig: a near pickup pays no fee
        parts["pickup_trip"] = trip
    else:  # its shipping price is not published: the estimate
        parts["shipping_estimate"], parts["finn_fee"] = estimate, fee
    if foreign:
        parts["vat"] = round((parts["price"] + parts.get("shipping", 0) + parts.get("shipping_estimate", 0)) * VAT, 2)
    money = ("price", "shipping", "shipping_estimate", "finn_fee", "pickup_trip", "vat")
    parts["penalties"] = dict(penalties or {})
    if listing.risk:  # a share of the money paid to that seller
        parts["penalties"][listing.risk] = round(RISK[listing.risk] * sum(parts.get(k, 0) for k in money), 2)
    parts["total"] = round(sum(parts.get(k, 0) for k in money) + sum(parts["penalties"].values()), 2)
    return parts, problem


class DailyFx:
    """Currency -> NOK rate from the ECB reference rates, fetched once per day per currency."""

    def __init__(self, fetch=http_json):
        self._fetch, self._cache, self._lock = fetch, {}, threading.Lock()

    def __call__(self, currency):
        if currency == "NOK":
            return 1.0
        key = (datetime.date.today(), currency)
        with self._lock:
            if key not in self._cache:
                r = self._fetch(f"https://api.frankfurter.dev/v1/latest?base={currency}&symbols=NOK")
                self._cache[key] = float(r["rates"]["NOK"])
            return self._cache[key]
