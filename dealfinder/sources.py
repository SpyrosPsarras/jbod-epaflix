"""Source adapters: a query in, Listings out. Each adapter normalises its Source's quirks (condition codes)."""
import base64
import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from .config import EBAY_PRICE_GBP, SOURCE_PAUSE_S

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
    description: str | None = None  # full listing text, fetched only where the rules need it
    location: str | None = None
    lat: float | None = None
    lon: float | None = None
    pickup_only: bool = False


def _get(url, headers=None, data=None):
    # some APIs (frankfurter) answer 403 to urllib's default User-Agent
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=30)


def http_json(url, headers=None, data=None):
    with _get(url, headers, data) as r:
        return json.load(r)


def http_text(url):
    with _get(url) as r:
        return r.read().decode("utf-8", "replace")


class _DescriptionText(HTMLParser):
    """Text of the element carrying data-testid="description" on a finn.no item page, one line per paragraph."""

    def __init__(self):
        super().__init__()
        self.depth, self.parts = 0, []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            self.depth += 1
            if tag in ("p", "br", "li", "div"):
                self.parts.append("\n")
        elif ("data-testid", "description") in attrs:
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\n\s*\n+", "\n", "".join(self.parts)).strip()


# sellers often promise shipping only in the text, not in finn's shipping flags
_FREE_SHIPPING = re.compile(r"\b(free\s+shipping|gratis\s+frakt|fri\s+frakt|frakt\s+inkludert|inkl\.?\s+frakt)\b", re.I)
_SHIPS = re.compile(r"\b(kan\s+(evt\.?\s+|også\s+)?sendes|sendes\s+mot|frakt\s+kan|sender\s+gjerne|can\s+ship)\b", re.I)


_NEGATION = re.compile(r"\b(ikke|not|no|nei|uten|without|dessverre)\b|\?", re.I)


# ponytail: crude 20-character window; "Gratis frakt, ikke henting" reads as negated. That errs to the safe side
# (charged a pickup trip or hidden), never to a fake bargain.
def _affirmed(rx, text):
    """True when `rx` matches without a negation just before or after it ("ikke gratis frakt", "frakt kan ikke")."""
    for m in rx.finditer(text):
        around = text[max(0, m.start() - 20):m.start()] + " " + text[m.end():m.end() + 20]
        if not _NEGATION.search(around):
            return True
    return False


def _shipping_from_text(listing):
    """Free or offered shipping stated in the description overrides a pickup-only reading of finn's flags."""
    text = listing.description or ""
    if _affirmed(_FREE_SHIPPING, text):
        listing.shipping, listing.pickup_only = 0.0, False
    elif listing.pickup_only and _affirmed(_SHIPS, text):
        listing.pickup_only = False  # shipping offered, price unknown: costed at the Fiks ferdig estimate


def _coordinate(value, limit):
    """A finite float within +-limit, else None (finn data is untrusted)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and abs(number) <= limit else None


class FinnSource:
    """finn.no Torget: the search page embeds its results as base64 JSON; item pages carry the full description.

    Condition comes from finn's own search filter, one request per bucket, so no item page is needed for it.
    """

    name = "finn"
    foreign = False
    supports_machines = True
    _search_url = "https://www.finn.no/recommerce/forsale/search"
    _buckets = (("new", ("1", "2")), ("used", ("3", "4")))  # 1 Helt ny, 2 Som ny, 3 Pent brukt, 4 Godt brukt

    def __init__(self, fetch=http_text, pause=SOURCE_PAUSE_S):
        self._fetch, self._pause = fetch, pause

    def search(self, query, details=False):
        listings, seen = [], set()
        for condition, codes in self._buckets:
            time.sleep(self._pause)
            qs = urllib.parse.urlencode([("q", query), *(("condition", c) for c in codes)])
            for doc in self._docs(self._fetch(f"{self._search_url}?{qs}")):
                try:
                    listing = self._listing(doc, condition)
                except (AttributeError, KeyError, TypeError, ValueError):
                    log.warning("skipping malformed finn doc %s", doc.get("id") if isinstance(doc, dict) else doc)
                    continue
                if listing and listing.source_id not in seen:
                    seen.add(listing.source_id)
                    listings.append(listing)
        if details:
            for listing in listings:
                listing.description = self._description(listing.url)
                _shipping_from_text(listing)
        return listings

    def _description(self, url):
        """Full text of one item page; None when it cannot be fetched (sold, removed), so one page never ends the Source."""
        if not url.startswith("https://www.finn.no/"):  # the URL comes from finn's data: fetch finn pages only
            return None
        time.sleep(self._pause)
        try:
            parser = _DescriptionText()
            parser.feed(self._fetch(url))
        except OSError as exc:  # urllib's HTTPError and URLError are OSErrors
            log.warning("finn item page %s unavailable: %s", url, exc)
            return None
        return parser.text() or None

    @staticmethod
    def _docs(html):
        for blob in re.findall(r"<script[^>]*>\s*(ey[A-Za-z0-9+/=\s]+?)\s*</script>", html):
            try:
                data = json.loads(base64.b64decode(blob))
            except ValueError:
                continue
            for q in (data.get("queries") or []) if isinstance(data, dict) else []:
                node = q
                for key in ("state", "data", "docs"):  # any level may be a list or missing
                    node = node.get(key) if isinstance(node, dict) else None
                if isinstance(node, list):
                    return node
        raise ValueError("finn search page carried no search results")

    def _listing(self, doc, condition):
        if doc.get("trade_type") != "Til salgs" or not doc.get("price"):
            return None  # giveaways and wanted-ads have no price to rank
        flags, coords = set(doc.get("flags") or []), doc.get("coordinates") or {}
        lat, lon = _coordinate(coords.get("lat"), 90), _coordinate(coords.get("lon"), 180)
        return Listing(
            source=self.name, source_id=str(doc["id"]), title=doc["heading"],
            url=doc.get("canonical_url") or f"https://www.finn.no/recommerce/forsale/item/{doc['id']}",
            price=float(doc["price"]["amount"]), currency=doc["price"].get("currency_code") or "NOK",
            shipping=0.0 if "seller_pays_shipping" in flags else None, shipping_currency="NOK",
            condition=condition, seller=None,
            location=doc.get("location"), lat=lat if lon is not None else None, lon=lon if lat is not None else None,
            pickup_only="shipping_exists" not in flags,
        )


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
    supports_machines = False  # eBay UK Machines arrive with ticket #11
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

    def search(self, query, details=False):
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
            except (AttributeError, KeyError, TypeError, ValueError):  # one malformed item must not drop the Source
                log.warning("skipping malformed eBay item %s", it.get("itemId") if isinstance(it, dict) else it)
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
