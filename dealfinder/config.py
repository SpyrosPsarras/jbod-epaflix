"""Scoring and search constants in one place (spec #1)."""

VAT = 0.25          # Norwegian import VAT on foreign Sources
MIN_DISK_TB = 14    # smallest Disk that qualifies
MIN_MACHINE_GEN = 13   # owner's Dell decoder: anything before 13th Gen is e-waste
MIN_MACHINE_BAYS = 8   # 3.5" bays a Machine needs
SOURCE_PAUSE_S = 1.0   # polite gap between requests to one Source
HUNT_INTERVAL_S = 6 * 3600  # a Hunt every 6 hours, counted from the last Hunt in the database

# eBay sorts by price and returns 100 results per query; the floor skips £1-£29 parts and
# accessories so those 100 slots go to real disks. Visible here on purpose, not hidden in the adapter.
EBAY_PRICE_GBP = (30, 2000)

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
    "dl380 gen9", "dl380 gen10", "supermicro server",
]
