# Deal Finder

Finds and ranks the cheapest way to buy a storage server and disks for epaflix, until the owner buys.

## Language

### What is bought

**Machine**:
A server that holds all pool disks internally: Dell 13th Gen or newer (or an HP/Supermicro equivalent), at least 8× 3.5" LFF bays, ECC RAM, working.
_Avoid_: Host, box, server (alone), JBOD

**Disk**:
A working 3.5" enterprise or NAS hard drive of at least 14TB.
_Avoid_: Drive, HDD (alone)

**Build**:
One Machine plus enough same-capacity Disks to reach at least 40 TiB usable in RAIDZ2. The unit that gets ranked.
_Avoid_: Deal, bundle, combo

### Where it comes from

**Listing**:
One offer for a Machine or Disks on a Source, at one moment in time.
_Avoid_: Ad, item, offer

**Source**:
A marketplace that is searched: finn.no, eBay UK, and AliExpress (Disks only, off until its Affiliate API is approved).
_Avoid_: Site, provider, shop

**Pickup trip**:
A round trip by car from Sandefjord to a pickup-only seller. Costs 4 NOK/km and is allowed up to 4 hours of driving in total; farther pickup-only Listings are hidden.
_Avoid_: Collection, travel

**Hunt**:
One pass over every Source that refreshes Listings and re-ranks Builds. Runs every 6 hours on its own, or when started by hand.
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

**High-risk Listing**:
A Disk Listing from AliExpress. Always tagged and carries a +20% risk Penalty. AliExpress Machines are out of scope.
_Avoid_: Risky listing, untrusted listing

### How it is ranked

**Landed cost**:
The full NOK price of a Listing at the owner's door: price, shipping to Norway (an eBay Machine with no freight quote to Norway is excluded), 25% import VAT when the Source is abroad, and the Pickup trip cost when the Listing is pickup-only, plus Penalties.
_Avoid_: Price, total

**Usable TiB**:
Capacity of the Build's RAIDZ2 pool: (disk count − 2) × disk size in TiB, minus pool overhead.
_Avoid_: Capacity, space

**Penalty**:
NOK added to a Build's Landed cost for a known weakness: the real cost to fix it (single PSU +500, per missing caddy +100, RAID-only controller +500, no rails +400) or a risk surcharge on the money paid to that seller (weak eBay seller under 98% positive or under 50 ratings +10%, an unstated rating counts as weak; High-risk Listing +20%).
_Avoid_: Deduction, malus

**Score**:
Landed cost of a Build divided by its Usable TiB. Lower is better.
_Avoid_: Rating, rank value

**Unreadable Listing**:
A Listing whose text does not state every fact the rules need (for a Machine: model, generation, 3.5" bay count, working condition; for a Disk: capacity, 3.5" form factor, enterprise/NAS class, condition), or that has no price. Shown under "Could not read", never ranked. A Listing whose known facts already rule it out (a 2TB drive, a 12th Gen server) is rejected instead.
_Avoid_: Unknown, unparsed, bad listing

**Gone Listing**:
A Listing that disappeared from its Source since the last Hunt, usually sold. Kept greyed out for 7 days with its last price.
_Avoid_: Sold, expired, deleted

**Ceiling**:
The highest Build Landed cost that is shown: 35,000 NOK. Builds above it are hidden.
_Avoid_: Budget, limit

**Bought**:
The owner's manual signal that the purchase is done. After it, Hunts stop.
_Avoid_: Done, closed
