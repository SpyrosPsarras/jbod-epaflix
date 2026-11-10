"""Ranking and search constants in one place (spec #1)."""

VAT = 0.25          # Norwegian import VAT on foreign Sources
SOURCE_PAUSE_S = 1.0   # polite gap between requests to one Source
# Pickup trips (ticket #5)
HOME_LAT_LON = (59.1312, 10.2166)  # Sandefjord
PICKUP_NOK_PER_KM = 4              # fuel, tolls, wear; round trip
PICKUP_MAX_MINUTES = 120           # one way; farther pickup-only Listings are hidden
# finn.no search results do not publish the Fiks ferdig price, so it is estimated per kind. RAM goes as a
# small parcel up to 5 kg (finn.no help center "Fiks ferdig - Slik velger du riktig pakkestørrelse": Helthjem 38,
# PostNord 39, Posten 55, PostNord 65 NOK); the highest, so it never underestimates.
FINN_SHIPPING_NOK = {"ram": 65}
# Trygg betaling, the buyer fee on every finn.no Fiks ferdig purchase: (fixed NOK, share of the price). An ESTIMATE
# fitted to one checkout (77 NOK on 800 NOK, 27 Sep 2026); finn.no does not publish the formula
FINN_BUYER_FEE = (29, 0.06)
# seller-risk surcharge: a share of the Listing's price + shipping or pickup trip + VAT (ticket #11)
RISK = {"weak_seller": 0.10, "seller_unknown": 0.10}
WEAK_SELLER = (98.0, 50)  # eBay seller under 98% positive or under 50 ratings is weak
ROUTE_FALLBACK = (1.3, 75)  # router down: straight-line km x 1.3 at 75 km/h (shown as an estimate)

HISTORY_WEEKS = 12      # weeks shown on the price history page
GONE_DAYS = 7          # a Gone Listing stays visible, greyed out, this many days
HUNT_INTERVAL_S = 6 * 3600  # a Hunt every 6 hours, counted from the last Hunt in the database

# eBay sorts by price and returns 100 results per query, so each kind gets a category and a GBP price range.
# Visible here on purpose, not hidden in the adapter. kind -> (category or None, (low, high))
EBAY_SEARCH = {
    # "Server Memory (RAM)" (checked 27 Sep 2026: 32 GB RDIMM from £50, 8x64 GB kits under £1,500). Browse takes one
    # category; "Memory (RAM)" 170083 holds a similar share of RDIMMs among desktop and laptop sticks
    "ram": ("11210", (5, 1500)),
}
# an eBay RAM Listing the title cannot read gets one item call for its aspects, at most once a day per Listing. The
# Browse quota is 5,000 calls a day, searches and item calls share that quota; a Hunt makes at most 20 item calls
EBAY_FACT_LOOKUPS = 20
EBAY_ITEM_MAX_AGE_S = 24 * 3600
# Rebuild IT (Asker) mirrors its web shop on finn.no and leaves sold-out ads up (a WD Gold 18TB ad from 2022, sold out
# in the shop, 28 Sep 2026). Its finn ads are checked against the shop's stock, read at most once an hour, and ship
# from the shop, free from this order total (rebuildit.no footer, 28 Sep 2026)
REBUILDIT_MAX_AGE_S = 3600
REBUILDIT_FREE_SHIPPING_NOK = 1000

# RAM (ticket #32): eBay queries name a size or speed to fit the 100 cheapest; finn.no is small
RAM_QUERIES = {
    "ebay_uk": ["ddr4 ecc rdimm 16gb", "ddr4 ecc rdimm 32gb", "ddr4 ecc rdimm 64gb", "ddr4 lrdimm 64gb",
                "ddr4 ecc reg 2400", "ddr4 ecc reg 2666", "ddr4 ecc reg 3200"],
    "finn": ["ddr4 ecc", "rdimm", "server minne", "ecc ram"],
}
# RAM_QUERIES are only the starting Tracked queries, copied into the database for each (kind, Source) group that has
# none yet; after that the page's Track button adds more (#8, #12)

# lowest believable Landed NOK per GB for used DDR4 RDIMM (16 GB DDR4-2133 lands at 9-13, 27 Sep 2026); below it a
# multi-stick Listing is priced per stick ("32GB x 10 stk" for 1,500 NOK) and counts as one stick
RAM_MIN_NOK_PER_GB = 8
MAX_QUERY_CHARS = 80
