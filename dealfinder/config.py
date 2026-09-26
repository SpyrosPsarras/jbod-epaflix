"""Scoring and search constants in one place (spec #1)."""

VAT = 0.25          # Norwegian import VAT on foreign Sources
MIN_DISK_TB = 14    # smallest Disk that qualifies

# eBay sorts by price and returns 100 results per query; the floor skips £1-£29 parts and
# accessories so those 100 slots go to real disks. Visible here on purpose, not hidden in the adapter.
EBAY_PRICE_GBP = (30, 2000)

DISK_QUERIES = [
    "exos 14tb", "exos 16tb", "exos 18tb", "exos 20tb",
    "ultrastar 16tb", "ultrastar 18tb", "ultrastar 20tb",
    "toshiba mg08 16tb", "toshiba mg09 18tb",
    "ironwolf pro 16tb", "wd red pro 16tb",
]
