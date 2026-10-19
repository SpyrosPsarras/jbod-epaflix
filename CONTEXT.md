# Deal Finder

Finds and ranks the cheapest compatible Disks and RAM for epaflix's Dell R730XD LFF, until the owner marks the purchase as Bought. The server has 12 front 3.5" bays, two rear 2.5" bays and 128 GB RAM; the owner plans a RAIDZ2 pool of 12TB Disks, grown one Disk at a time.

## Language

### What is bought

**Disk**:
A working, genuine 3.5" SATA or SAS hard drive of at least 12TB, of any class, including enterprise, NAS, desktop, surveillance or an unstated class. External USB enclosures and 2.5" disks do not qualify.
_Avoid_: Drive, HDD (alone)

**RAM**:
Working DDR4 ECC RDIMM or LRDIMM server memory for the R730XD, sold as individual sticks or a lot.
_Avoid_: Part, component, add-on

### Where it comes from

**Listing**:
One offer for Disks or RAM on a Source, at one moment in time. A Disk Listing sells a whole lot at its stated price, or one Disk at a per-Disk price with any stated stock.
_Avoid_: Ad, item, offer

**Source**:
A marketplace that is searched: finn.no and eBay UK.
_Avoid_: Site, provider, shop

**Pickup trip**:
A round trip by car from Sandefjord to a pickup-only seller. Costs 4 NOK/km and is allowed up to 4 hours of driving in total; farther pickup-only Listings are hidden.
_Avoid_: Collection, travel

**Hunt**:
One pass over every Source that refreshes Disk and RAM Listings, every 6 hours or when started by hand. A restart never starts one: a Hunt overdue at start-up runs 6 hours later, unless started by hand.
_Avoid_: Scan, crawl, job, run

**Search**:
A free-text query the owner types on the page, run against every Source at once. Results are shown immediately and are not kept unless the query is tracked.
_Avoid_: Lookup, manual hunt

**Tracked query**:
A search term that every Hunt runs. Created from a Search with one click.
_Avoid_: Saved search, keyword, watch

**Source fault**:
A Source that failed during the last Hunt, or returned no Listings for any Tracked query. Shown as a warning on the page.
_Avoid_: Error, alert

### How it is ranked

**Landed cost**:
The full NOK price of a Listing at the owner's door: price, shipping to Norway, 25% import VAT when the Source is abroad, the Pickup trip cost when applicable, the estimated finn.no Trygg betaling buyer fee when shipped via Fiks ferdig, and any Seller-risk surcharge. Rebuild IT sells through its own web shop and pays no Trygg betaling fee; Disks rank by Landed cost per TB, RAM by Landed cost per GB, cheapest first.
_Avoid_: Price, total

**Seller-risk surcharge**:
An extra 10% of the money paid to that seller, including price, shipping or Pickup trip, VAT and fee, for an eBay seller under 98% positive or under 50 ratings. An unstated rating also earns the surcharge.
_Avoid_: Penalty, deduction, malus

**Unreadable Listing**:
A Listing missing a price or facts the rules need: Disk capacity, 3.5" form factor and condition, or RAM stick size, DDR generation and RDIMM/LRDIMM type. Shown under "Could not read", never ranked; a known disqualifying fact, such as a 10TB Disk, rejects it instead.
_Avoid_: Unknown, unparsed, bad listing

**Gone Listing**:
A Listing that disappeared from its Source since the last successful Hunt, kept greyed out for 7 days with its last price. A Rebuild IT Listing disappears when its web shop shows it sold out, because its finn.no Listings may remain online; its in-stock Listings ship, free from 1,000 NOK.
_Avoid_: Sold, expired, deleted

**Deal**:
A live ranked Listing whose Landed cost per TB or GB is below the lowest price its group had before the current week, across all Sources within the price history page's weeks. The group is the same Disk capacity or RAM DDR generation, type and stick size; fewer than 5 Listings gives no Deals. Only a Listing below that earlier lowest price shows "That's a deal".
_Avoid_: Bargain, steal

**Bought**:
The owner's manual signal that the purchase is done. After it, Hunts stop.
_Avoid_: Done, closed
