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
# finn.no search results do not publish the Fiks ferdig price, so it is estimated per kind. Parts and Disks go as a
# small parcel up to 5 kg (finn.no help center "Fiks ferdig - Slik velger du riktig pakkestørrelse": Helthjem 38,
# PostNord 39, Posten 55, PostNord 65 NOK); the highest, so it never underestimates. A Machine is a heavy parcel
# (owner's estimate)
FINN_SHIPPING_NOK = {"machine": 400, "disk": 65, "cpu": 65, "ram": 65, "heatsink": 65}
# Trygg betaling, the buyer fee on every finn.no Fiks ferdig purchase: (fixed NOK, share of the price). An ESTIMATE
# fitted to one checkout (77 NOK on 800 NOK, 27 Sep 2026); finn.no does not publish the formula
FINN_BUYER_FEE = (29, 0.06)
PENALTY_NOK = {"single_psu": 500, "caddy": 100, "raid_only": 500, "no_rails": 400}
RAM_TARGET_GB = 128   # RAM a Build is completed up to; more earns no credit
# ponytail: DIMM slots per socket, assumed (R730/R740/DL380 have 12); read the model's real count if a Build hits it
RAM_SLOTS_PER_SOCKET = 12
# risk Penalties: a share of the Listing's price + shipping or pickup trip + VAT (ticket #11)
RISK = {"weak_seller": 0.10, "seller_unknown": 0.10}
WEAK_SELLER = (98.0, 50)  # eBay seller under 98% positive or under 50 ratings is weak
ROUTE_FALLBACK = (1.3, 75)  # router down: straight-line km x 1.3 at 75 km/h (shown as an estimate)

# Builds (ticket #6)
TARGET_TIB = 40          # usable RAIDZ2 capacity a Build must reach
POOL_OVERHEAD = 0.05     # ZFS metadata and slop taken off raw RAIDZ2 capacity
CEILING_NOK = 40_000     # Builds above this Landed cost are hidden (35,000 until complete Builds with real Parts)
MIN_BUILD_DISKS = 4      # smallest RAIDZ2 layout considered
BOOT_BAYS = 1            # one 3.5" bay kept for the boot SSD

HISTORY_WEEKS = 12      # weeks shown on the price history page
GONE_DAYS = 7          # a Gone Listing stays visible, greyed out, this many days
HUNT_INTERVAL_S = 6 * 3600  # a Hunt every 6 hours, counted from the last Hunt in the database

# eBay sorts by price and returns 100 results per query, so each kind gets a category and a GBP price range.
# Visible here on purpose, not hidden in the adapter. kind -> (category or None, (low, high))
EBAY_SEARCH = {
    "disk": (None, (30, 2000)),        # the floor skips £1-£29 parts and accessories
    "machine": ("11211", (100, 2500)),  # "Computer Servers" keeps rails, PSUs and other parts out
    # "CPUs/Processors" (checked 27 Sep 2026: E5-2680 v4 from £10, EPYC 7302 from £30, Silver 4310 £440-£515);
    # the £3 floor skips £1 junk, £600 keeps 3rd Gen Xeon in reach
    "cpu": ("164", (3, 600)),
    # "Server Memory (RAM)" (checked 27 Sep 2026: 32 GB RDIMM from £50, 8x64 GB kits under £1,500). Browse takes one
    # category; "Memory (RAM)" 170083 holds a similar share of RDIMMs among desktop and laptop sticks
    "ram": ("11210", (5, 1500)),
    # server heatsinks sit in "Server Fans & Cooling Systems" 168074 and "CPU Fans & Heatsinks" 131486 (checked
    # 27 Sep 2026: each holds only part of "r730 heatsink", 12 and 18 of 54), so no category; the reader drops
    # the laptop coolers and fans. R730 and DL380 Gen9 heatsinks from about £9, the £2 floor keeps them in reach
    "heatsink": (None, (2, 150)),
}

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
}

MACHINE_QUERIES = [
    "r730xd", "r740xd", "r730", "r740", "r540",
    "dl380 gen9", "dl380 gen10", "supermicro server", "supermicro 12 bay",
]
# CPUs (ticket #31): eBay needs model numbers to fit its 100 cheapest; finn.no is small, so family words.
# 3rd Gen Xeon under £600 is rare on eBay UK (5318Y and 6338: none on 27 Sep 2026), Silver 4310 has some
CPU_QUERIES = {
    "ebay_uk": ["e5-2680 v4", "e5-2690 v4", "e5-2650 v4", "e5-2660 v4", "xeon gold 6130", "xeon silver 4210",
                "xeon gold 6230", "xeon silver 4310", "epyc 7302"],
    "finn": ["xeon e5", "xeon gold", "xeon silver", "epyc"],
}
# RAM (ticket #32): eBay queries name a size or speed to fit the 100 cheapest; finn.no is small
RAM_QUERIES = {
    "ebay_uk": ["ddr4 ecc rdimm 16gb", "ddr4 ecc rdimm 32gb", "ddr4 ecc rdimm 64gb", "ddr4 lrdimm 64gb",
                "ddr4 ecc reg 2400", "ddr4 ecc reg 2666", "ddr4 ecc reg 3200"],
    "finn": ["ddr4 ecc", "rdimm", "server minne", "ecc ram"],
}
# lowest believable Landed NOK per GB for used DDR4 RDIMM (16 GB DDR4-2133 lands at 9-13, 27 Sep 2026); below it a
# multi-stick Listing is priced per stick ("32GB x 10 stk" for 1,500 NOK) and counts as one stick
RAM_MIN_NOK_PER_GB = 8
# heatsinks (ticket #33): one eBay query per common Machine family; finn.no is small, so generic words
HEATSINK_QUERIES = {
    "ebay_uk": ["r730 heatsink", "r730xd heatsink", "r740 heatsink", "r740xd heatsink", "r630 heatsink",
                "dl380 gen9 heatsink", "dl380 gen10 heatsink", "dl360 gen9 heatsink", "r540 heatsink",
                "supermicro 2u heatsink"],
    "finn": ["kjøleribbe server", "heatsink", "kjøler dell", "kjøler hp"],
}
# DISK_QUERIES, MACHINE_QUERIES, CPU_QUERIES, RAM_QUERIES and HEATSINK_QUERIES are only the starting Tracked queries,
# copied into the database for each (kind, Source) group that has none yet; after that the page's Track button adds
# more (#8, #12)

# CPU socket per Machine platform (amd, generation); 16th Gen and newer (DDR5) are not supported: None.
# Vendor does not change the socket: a 13th Gen PowerEdge, ProLiant Gen9 and Supermicro X10 are all LGA2011-3
PLATFORMS = {(False, 13): "LGA2011-3", (False, 14): "LGA3647", (False, 15): "LGA4189",
             (True, 14): "SP3", (True, 15): "SP3"}
MAX_QUERY_CHARS = 80
