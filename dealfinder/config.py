"""Scoring and search constants in one place (spec #1)."""

VAT = 0.25          # Norwegian import VAT on foreign Sources
MIN_DISK_TB = 14    # smallest Disk that qualifies
MIN_MACHINE_GEN = 13   # owner's Dell decoder: anything before 13th Gen is e-waste
MIN_MACHINE_BAYS = 8   # 3.5" bays a Machine needs
SOURCE_PAUSE_S = 1.0   # polite gap between requests to one Source
# Pickup trips and Penalties (ticket #5)
HOME_LAT_LON = (59.1312, 10.2166)  # Sandefjord
PICKUP_NOK_PER_KM = 4              # fuel, tolls, wear; round trip
PICKUP_MAX_MINUTES = 120           # one way; farther pickup-only Listings are hidden
# finn.no search results do not publish the Fiks ferdig price; estimate for a heavy parcel
FINN_SHIPPING_NOK = 400
PENALTY_NOK = {"single_psu": 500, "caddy": 100, "raid_only": 500, "no_rails": 400,
               "cpu": 500}  # a pair of E5-26xx v4: £23 on eBay UK + VAT (27 Sep 2026)
RAM_TARGET_GB = 128   # RAM a Machine is priced up to; more earns no credit
RAM_NOK_PER_GB = 50   # used DDR4 ECC RDIMM: finn.no 32 GB for 1,400-2,000 NOK, 4x32 GB for 6,000 (27 Sep 2026)
# risk Penalties: a share of the Listing's price + shipping or pickup trip + VAT (ticket #11, #12)
RISK = {"weak_seller": 0.10, "seller_unknown": 0.10, "high_risk": 0.20}
WEAK_SELLER = (98.0, 50)  # eBay seller under 98% positive or under 50 ratings is weak
ROUTE_FALLBACK = (1.3, 75)  # router down: straight-line km x 1.3 at 75 km/h (shown as an estimate)

# Builds (ticket #6)
TARGET_TIB = 40          # usable RAIDZ2 capacity a Build must reach
POOL_OVERHEAD = 0.05     # ZFS metadata and slop taken off raw RAIDZ2 capacity
CEILING_NOK = 35_000     # Builds above this Landed cost are hidden
MIN_BUILD_DISKS = 4      # smallest RAIDZ2 layout considered
BOOT_BAYS = 1            # one 3.5" bay kept for the boot SSD

HISTORY_WEEKS = 12      # weeks shown on the price history page
GONE_DAYS = 7          # a Gone Listing stays visible, greyed out, this many days
HUNT_INTERVAL_S = 6 * 3600  # a Hunt every 6 hours, counted from the last Hunt in the database

# eBay sorts by price and returns 100 results per query; the floor skips £1-£29 parts and
# accessories so those 100 slots go to real disks. Visible here on purpose, not hidden in the adapter.
EBAY_PRICE_GBP = (30, 2000)
# eBay Machines: category 11211 "Computer Servers" keeps rails, PSUs and other parts out of the 100 slots
EBAY_MACHINE_PRICE_GBP = (100, 2500)
EBAY_MACHINE_CATEGORY = "11211"

# per Source: eBay returns only the 100 cheapest per query, so its queries name a capacity; finn.no search is
# token based ("16tb" misses "16 tb") and small, so family words find more there
DISK_QUERIES = {
    "ebay_uk": [
        "exos 14tb", "exos 16tb", "exos 18tb", "exos 20tb",
        "ultrastar 16tb", "ultrastar 18tb", "ultrastar 20tb",
        "toshiba mg08 16tb", "toshiba mg09 18tb",
        "ironwolf pro 16tb", "wd red pro 16tb",
    ],
    "finn": ["exos", "ultrastar", "ironwolf pro", "wd red pro", "toshiba", "14tb", "16tb", "18tb", "20tb"],
    "aliexpress": ["exos 16tb", "exos 18tb", "exos 20tb", "ultrastar 18tb", "toshiba mg09 18tb"],
}
# AliExpress (ticket #12): the Affiliate API's product search publishes no freight price, so Disks carry this
# visible estimate for tracked shipping to Norway
ALIEXPRESS_SHIPPING_NOK = 150

MACHINE_QUERIES = [
    "r730xd", "r740xd", "r730", "r740", "r540",
    "dl380 gen9", "dl380 gen10", "supermicro server", "supermicro 12 bay",
]
# DISK_QUERIES and MACHINE_QUERIES are only the starting Tracked queries, copied into the database for each
# (kind, Source) group that has none yet; after that the page's Track button adds more (tickets #8, #12)
MAX_QUERY_CHARS = 80
