"""Landed cost in NOK and the daily exchange rate."""
import datetime
import threading

from .config import VAT
from .sources import http_json


def landed_nok(listing, fx, foreign):
    """Price plus shipping to Norway in NOK, with import VAT for foreign Sources. None when shipping is unknown."""
    if listing.shipping is None:
        return None
    total = listing.price * fx(listing.currency) + listing.shipping * fx(listing.shipping_currency or listing.currency)
    return round(total * (1 + VAT) if foreign else total, 2)


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
