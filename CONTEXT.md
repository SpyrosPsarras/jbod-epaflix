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

**Part**:
A CPU, RAM or Heatsink Listing that completes a Machine. A heatsink Listing names the Machine models it fits.
_Avoid_: Component, add-on

**Build**:
One Machine, plus the Parts it lacks (a CPU of one model in every socket, RAM up to 128 GB, a heatsink for every added CPU), plus enough same-capacity Disks to reach at least 40 TiB usable in RAIDZ2. One Disk Listing can supply several of those Disks: a lot ("4x 16TB") whole at its price, or a single disk up to the stock eBay states or a finn text priced per disk states ("Selger 4 stk. Pris per stk"); each Disk after the first pays its price, extra shipping and VAT or fee again. The unit that gets ranked. A Build whose Parts are not on sale is hidden.
_Avoid_: Bundle, combo (a Deal is one Listing)

### Where it comes from

**Listing**:
One offer for a Machine, Disks, CPUs, RAM or heatsinks on a Source, at one moment in time. Each configuration of an eBay Machine sold as variations ("choose your configuration") is its own Listing, with its own price, specs and link; of configurations the rules read alike, only the cheapest is kept.
_Avoid_: Ad, item, offer

**Source**:
A marketplace that is searched: finn.no and eBay UK.
_Avoid_: Site, provider, shop

**Pickup trip**:
A round trip by car from Sandefjord to a pickup-only seller. Costs 4 NOK/km and is allowed up to 4 hours of driving in total; farther pickup-only Listings are hidden.
_Avoid_: Collection, travel

**Hunt**:
One pass over every Source that refreshes Listings and re-ranks Builds. Runs every 6 hours on its own, or when started by hand. A restart never starts one: a Hunt overdue at start-up runs 6 hours later, unless started by hand.
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
The full NOK price of a Listing at the owner's door: price, shipping to Norway (an eBay Machine with no freight quote to Norway is excluded), 25% import VAT when the Source is abroad, the Pickup trip cost when the Listing is pickup-only, and the finn.no Trygg betaling buyer fee (estimated) when a finn.no Listing is shipped via Fiks ferdig (not for Rebuild IT, which sells through its own web shop), plus Penalties.
_Avoid_: Price, total

**Usable TiB**:
Capacity of the Build's RAIDZ2 pool: (disk count − 2) × disk size in TiB, minus pool overhead.
_Avoid_: Capacity, space

**Penalty**:
NOK added to a Build's Landed cost for a known weakness: the real cost to fix it (+500 per PSU short of two: single PSU +500, no PSU +1,000, unstated count +500; per missing caddy +100, RAID-only controller +500, no rails +400; missing CPUs and RAM are bought as Parts instead) or a risk surcharge on the money paid to that seller (weak eBay seller under 98% positive or under 50 ratings +10%, an unstated rating counts as weak).
_Avoid_: Deduction, malus

**Completed cost**:
A Machine's Landed cost plus the cheapest CPUs, RAM and heatsinks on sale that it lacks, the same Parts its Build buys. Best Machines ranks by it, so a bare chassis does not beat a full server just by missing Parts. A Machine whose Parts are not on sale has none and ranks last.
_Avoid_: Full price, total

**Score**:
Landed cost of a Build divided by its Usable TiB. Lower is better.
_Avoid_: Rating, rank value

**Unreadable Listing**:
A Listing whose text does not state every fact the rules need (for a Machine: model, generation, 3.5" bay count, working condition; for a Disk: capacity, 3.5" form factor, enterprise/NAS class, condition), or that has no price. Shown under "Could not read", never ranked. A Listing whose known facts already rule it out (a 2TB drive, a 12th Gen server) is rejected instead.
_Avoid_: Unknown, unparsed, bad listing

**Gone Listing**:
A Listing that disappeared from its Source since the last Hunt, usually sold. Kept greyed out for 7 days with its last price. A finn.no Listing from Rebuild IT counts as disappeared when its web shop (rebuildit.no) shows that product sold out, because Rebuild IT leaves sold-out Listings up on finn.no. Its other Listings ship, free from 1,000 NOK.
_Avoid_: Sold, expired, deleted

**Deal**:
A live ranked Listing whose Landed cost per unit (per TB, CPU, GB or heatsink; a Machine as a whole) is under the lowest price its group had before the current week: same Disk capacity, Machine model or Part key, all Sources, within the price history page's weeks. A group seen in fewer than 5 Listings in those weeks has no Deals. Highlighted with "That's a deal".
_Avoid_: Bargain, steal

**Ceiling**:
The highest Build Landed cost that is shown: 40,000 NOK. Builds above it are hidden.
_Avoid_: Budget, limit

**Bought**:
The owner's manual signal that the purchase is done. After it, Hunts stop.
_Avoid_: Done, closed
