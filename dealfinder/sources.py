"""Source adapters: a query in, Listings out. Each adapter normalises its Source's quirks (condition codes)."""
import base64
import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import EBAY_PRICE_GBP

log = logging.getLogger("dealfinder.sources")

USER_AGENT = "jbod-epaflix-dealfinder/0.1 (+https://github.com/SpyrosPsarras/jbod-epaflix)"


@dataclass
class Listing:
    source: str
    source_id: str
    title: str
    url: str
    price: float
    currency: str
    shipping: float | None  # None = unknown
    shipping_currency: str | None
    condition: str | None   # one of rules.CONDITIONS, None = the Source did not say
    seller: str | None


def http_json(url, headers=None, data=None):
    # some APIs (frankfurter) answer 403 to urllib's default User-Agent
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# eBay conditionId -> normalised condition
_EBAY_CONDITION = {
    **dict.fromkeys(("1000", "1500", "1750", "2750"), "new"),
    **dict.fromkeys(("2000", "2010", "2020", "2030", "2500"), "refurbished"),
    **dict.fromkeys(("3000", "4000", "5000", "6000"), "used"),
    "7000": "for_parts",
}


class EbaySource:
    """eBay UK via the Browse API: Buy It Now, ships to Norway, marketplace EBAY_GB."""

    name = "ebay_uk"
    foreign = True
    _token_url = "https://api.ebay.com/identity/v1/oauth2/token"
    _search_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, client_id, client_secret, fetch=http_json):
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self._fetch = fetch
        self._token, self._token_expiry = None, 0.0

    def _auth(self):
        if time.time() > self._token_expiry - 60:
            body = b"grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope"
            r = self._fetch(self._token_url, {"Authorization": "Basic " + self._basic,
                                              "Content-Type": "application/x-www-form-urlencoded"}, body)
            self._token, self._token_expiry = r["access_token"], time.time() + int(r.get("expires_in", 7200))
        return self._token

    def search(self, query):
        low, high = EBAY_PRICE_GBP
        qs = urllib.parse.urlencode({
            "q": query, "sort": "price", "limit": "100",
            "filter": f"buyingOptions:{{FIXED_PRICE}},deliveryCountry:NO,price:[{low}..{high}],priceCurrency:GBP"})
        r = self._fetch(f"{self._search_url}?{qs}", {
            "Authorization": "Bearer " + self._auth(),
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DNO"})
        listings = []
        for it in r.get("itemSummaries", []):
            try:
                listings.append(self._listing(it))
            except (KeyError, TypeError, ValueError):  # one malformed item must not drop the whole Source
                log.warning("skipping malformed eBay item %s", it.get("itemId"))
        return listings

    def _listing(self, it):
        ship = ((it.get("shippingOptions") or [{}])[0]).get("shippingCost") or {}
        return Listing(
            source=self.name, source_id=it["itemId"], title=it["title"],
            url=it.get("itemWebUrl", "").split("?")[0],
            price=float(it["price"]["value"]), currency=it["price"]["currency"],
            shipping=float(ship["value"]) if "value" in ship else None,
            shipping_currency=ship.get("currency"),
            condition=_EBAY_CONDITION.get(it.get("conditionId")),
            seller=(it.get("seller") or {}).get("username"),
        )
