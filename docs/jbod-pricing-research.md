# JBOD research for epaflix — prices and build options

Research date: 2026-09-19. Prices verified against live eBay UK category pages, the eBay Browse API (2026-09-26, used disks), and Amazon listings via diskprices.com unless marked **unverified estimate** or **secondary source**.
FX on 2026-09-18 (ECB): £1 = 12.59 NOK, $1 = 9.43 NOK.

## TL;DR

A used NetApp DS4246 24-bay shelf + LSI HBA + used enterprise disks was the paper-cheapest path to 40+ TB usable with swappable disks, but the TrueNAS host has no free PCIe slot (verified 2026-09-19 via SSH), so the shelf paths die. **Selected: Path B1 — a Dell PowerEdge R730xd, 13th Gen (5,000 NOK, Oslo, verified on finn.no, 12-bay LFF + 2 SFF) becomes the new TrueNAS host**, plus 6×16TB Exos recertified disks (3,000-3,500 NOK each, finn.no, verified) in RAIDZ2 ≈ 55 TiB usable. Total ~24.5-28k NOK, all-local, no import. Alternates: the Nedenes 8-bay R730 (12,000, free shipping) and the Trondheim R730xd (13,750, turnkey). The R720xd at 9,000 NOK was demoted by the owner's Dell PowerEdge decoder (12th Gen = e-waste tier). Full reasoning and the live option table in "Big server vs JBOD".

## Current state of epaflix storage (from the repo)

- F1: `pool1` on TrueNAS (192.168.10.200) is a **2-disk non-redundant stripe** (10TB IronWolf + 14TB Exos, ~21.8 TiB total), holding all media. Source: epaflix issue #401; drive models verified via SSH 2026-09-19.
- F2: At the Aug 2026 decision it was 88% used (`21.8T size, 19.3T alloc, 2.46T free`). Source: #805 comment 2026-08-21.
- F3: Owner decision 2026-08-23: no disk budget for ~6 months (until ~2027-02); headroom comes from a media deletion service + retention. Source: #805 comments.
- F4: Growth and runway are already measured weekly by `0-truenas/scripts/pool-capacity-forecast.py` (cron Mondays 04:15, thresholds 90/95%). Source: #805 comment 2026-09-06.
- F5: PBS backups no longer depend on pool1 (moved to takaros local-raid). Source: #401.
- Q1: The TrueNAS box's internals (case type, free PCIe slots, free drive bays, PSU) are **not documented anywhere in the repo**. This decides whether an internal HBA option exists. Check on the box: `lspci`, `dmesg | grep -i sas`, case layout.

## Why an external shelf beats the alternatives

- O1 (**recommended**): Used JBOD shelf (NetApp DS4246, 24×3.5" hot-swap) + LSI HBA in the TrueNAS box + SAS cable. Cheap, 24 swappable bays, grows with you, no new machine to maintain. TrueNAS handles SAS expander shelves natively.
- O2: Internal only (HBA + the TrueNAS box's own bays). Cheapest if the box is a tower with free slots and bays, but capped by unknown bay count. Needs Q1 answered first.
- O3: Second server as storage node. Extra CPU, RAM, and power for no gain when the goal is just disk attached to TrueNAS.
- O4: Consumer NAS / rack NAS. Poor value: a new 4-bay Synology RS820RP+ was £900 on eBay UK (live 2026-09-19) vs the DS4246 at £300 for 24 bays.

## Verified prices

### Shelf / enclosure (eBay UK, live 2026-09-19, sources: category pages bn_2751703 and bn_25881125)

| Item | Price | Notes |
|---|---|---|
| NetApp DS4246 24-bay, 2× IOM6, 2× PSU | £299.99 or Best Offer, 17 sold | +£101 P&P; the workhorse pick |
| NetApp DS4246 (Compan-IT store) | £213.99 | **unverified estimate**: seen in a search-engine snippet of the store page, not in the fetched category pages |
| NetApp DS4246 | £249 | **secondary source**: r/DataHoarder purchase, Aug 2024 |
| Dell PowerVault MD1400 12×3.5", 2× 12G SAS-4 | £527.32 | +£34 P&P; newer backplane |
| NetApp DS212C 12×LFF, IOM12 | £173.51 | 12 bays |
| NetApp FAS2750 24×2.5" | £76.74 | 2.5" only, useless for big HDDs |
| Synology RS820RP+ 4-bay, new | £900 | Shows why consumer NAS loses |

Pricing reference: r/DataHoarder Aug 2024 DS4246 at £249.

### HBA (eBay UK, live 2026-09-19, source: category page bn_25881125)

| Card | Price | Fit |
|---|---|---|
| LSI 9300-8i/16i/9305-16i/9305-24i IT mode (variants) | £39.88-125 | 8i = internal only |
| LSI 9305-16e, brand new, 16-port external | £59.88, free intl postage | Best for a shelf |
| LSI 9240-8i (=9211-8i) + 2 cables | £29.88, 314 sold | Internal, 6G, proven |
| LSI 9200-8i (=9211-8i) IT mode | £24.98, 260 sold | Internal, 6G |
| LSI 9212-4i IT mode | £12.98 | Internal, 6G, 4 ports |
| LSI 9300-16i | £8-60 (variants) | Internal, 12G |

For an external shelf you need an **-e** card (external miniSAS). The 9305-16e at £59.88 new is the standout. A 9300-8e also works and typically lists £40-60; that price is an **unverified estimate** for eBay UK today.

### Cables

SFF-8088→SFF-8644 (for a 9200-8e/9300-8e → DS4246 IOM6) typically £8-20 each, **unverified estimate**; buy 1-2. The 9305-16e outputs SFF-8644 directly, so SFF-8644→SFF-8644 works and is also cheap.

### Disks (verified, Amazon UK "Renewed" via diskprices.com UK locale, 2026-09-19)

| Drive | Price | £/TB |
|---|---|---|
| Toshiba MG07ACA14TE 14TB (renewed) | £295 | £21.06 |
| HGST Ultrastar HC520 12TB (refurb) | £310 | £25.83 |
| Seagate Exos X24 24TB (renewed) | £635 | £26.47 |
| Seagate Exos 26TB (renewed) | £599 | £23.04 |
| Seagate Exos 28TB (renewed) | £659 | £23.54 |
| Seagate IronWolf Pro 18TB (renewed) | £595 | £33.06 |

US anchors (Amazon, diskprices.com US locale): Toshiba MG07ACA14TE 14TB used $319 ($22.79/TB); Seagate 16TB SAS (ST16000NM007H) used $371 ($23.21/TB), a true Exos X16 SAS lists $359 ($25.64/TB); WD Ultrastar 20TB (WD200EDGZ, renewed) $560 ($28.00/TB). **eBay UK used 16TB, verified 2026-09-26 via the eBay Browse API** (Buy It Now, ships to Norway, working condition only, n=85): cheapest £288 delivered, median £488. That is ~4,500 NOK (cheapest) to ~7,700 NOK (median) landed with 25% VAT. The sub-£130 listings are all "for parts or not working". eBay UK is **not** cheaper than finn.no for disks.

### finn.no

finn.no search pages block generic scraping, but the finn_mcp CLI (github:viktorfa/finn_mcp, wired into opencode as an MCP server) queries Torget directly. Live results 2026-09-19, all **verified**:

| Listing | Price | Location | finn code |
|---|---|---|---|
| Seagate Exos 16TB recertified (multiple units) | 3000-3500 NOK | Bygstad | 476514743, 476515083, 476646797, 476668846, 476667131, 476667225 |
| WD Ultrastar DC HC550 18TB SATA, SMART OK | 5250 NOK | Oslo | 476558307 |
| Seagate Exos X18 18TB | 8000 NOK | Espeland | 476522682 |
| Dell PowerVault MD1400 12×8TB SAS, dual PSU (filled) | 24900 NOK | Porsgrunn | 454798031 |
| Supermicro SuperServer rack JBOD 4U | 14000 NOK | Bergen | 421957363 |
| Dell EMC KTN-STL3 15-bay with 15×4TB SAS | 10000 NOK | Stavanger | 475415850 (posted 2026-09-02) |

Reads:

- finn.no at 3000-3500 NOK per 16TB ≈ £14.9-17.4/TB. Cheaper than the cheapest working eBay UK 16TB (£18/TB delivered, before VAT) and the Amazon renewed anchors, with zero import friction.
- **No DS4246 on finn.no today** (searched 2026-09-19). The KTN-STL3 chassis-only angle only works haggled well below 10 000 kr.
- The Exos X18 at 8000 NOK (£35/TB) is overpriced; the HC550 18TB at 5250 NOK (£23/TB) is the sane local 18TB option.
- A domestic S2 variant (6×16TB local at 18000-21000 NOK) is the cheapest verified disk source: working 16TB drives on eBay UK land at ~4,500-7,700 NOK each (verified 2026-09-26), versus 3,000-3,500 NOK on finn.no.

## Budget scenarios (shelf + HBA + cables + disks)

Usable math: RAIDZ2 of N disks gives (N-2)×disk size, minus ~5% ZFS overhead. A 16TB drive = 14.55 TiB; 24TB = 21.8 TiB.

| Scenario | Disks | Raw | Usable | Disk cost | Total ex-VAT | Landed in Norway (×1.25 VAT) |
|---|---|---|---|---|---|---|
| S1 minimum target | 4×24TB renewed Exos @ £635 | 96TB | ~41 TiB | £2,540 | ~£2,850 | ~45k NOK |
| S2 via eBay UK (superseded) | 6×16TB working used, eBay UK cheapest £288 delivered (verified 2026-09-26) | 96TB | ~55 TiB | ~£1,730+ | ~£2,100+ | ~33k+ NOK (median-priced disks: ~52k) |
| S2 worst case | 6×16TB at Amazon £/TB ceiling (£26/TB) | 96TB | ~55 TiB | ~£2,520 | ~£2,850 | ~45k NOK |
| S2-NO all-domestic disks | 6×16TB Exos recertified, finn.no @ 3000-3500 NOK (verified) + DS4246/HBA from UK | 96TB | ~55 TiB | 18-21k NOK | ~23-26k NOK | 24-27k NOK (VAT only on UK parts) |
| S3 headroom | 6×24TB renewed Exos @ £635 | 144TB | ~83 TiB | £3,810 | ~£4,100 | ~64k NOK |
| S3 alt | 6×26TB renewed @ £599 | 156TB | ~90 TiB | £3,594 | ~£3,900 | ~61k NOK |

Fixed parts for every scenario: DS4246 £299.99 verified (as low as ~£214 if the Compan-IT listing is real) + HBA £40-60 + cables £20-40 ≈ £360-400 ex-VAT.

Notes on the scenarios:

- The earlier £130-160 eBay estimate for S2 was wrong: the eBay API check on 2026-09-26 found only "for parts" drives at that price. Buy the disks on finn.no (S2-NO).
- S3 has the best verified £/TB and the most runway; it spends the whole thing in one shot though.
- Norway import: expect 25% VAT either collected at checkout (VOEC, when the order is ≤3000 NOK) or at import (Posten adds a handling fee). The landed column rounds this up.
- Power: a DS4246 with 2 PSUs and 8-24 spinning disks idles at roughly 80-150W. At ~1 NOK/kWh that is ~700-1300 kWh/year, 700-1300 NOK/year. Remove one PSU on a single-host setup to trim idle draw.

## Big server vs JBOD chassis (the buying decision)

Question from the owner 2026-09-19: big form-factor server or JBOD for 40+ TB, weights on price and availability. All finn.no prices **verified live 2026-09-19** via the finn MCP CLI:

| Local option (finn.no) | Price | What it is |
|---|---|---|
| Dell PowerEdge R730xd (13th Gen), 2× E5-2650v3, 128GB DDR4 | 5,000 NOK | Oslo 473386139; **12× LFF + 2× SFF bays**, PERC H730 (flash to IT mode for ZFS), X520 2× 10G SFP+ + 2× 1G, **single PSU**, **no caddies** (~1k extra for 12), pickup Oslo |
| Dell PowerEdge R730xd (13th Gen), 2× E5-2680v4, 128GB DDR4 | 13,750 NOK | Trondheim 468970308; **12× LFF + 2× SFF**, 28C/56T, 2× 240GB boot SSDs included, dual 1100W Platinum, iDRAC8 Enterprise, rails + bezel, 6 of 12 LFF caddies included, pickup only |
| Dell PowerEdge R730xd (13th Gen), 384GB, 28 cores | 14,500 NOK | 474398976; **16× 3.5" + 2× 2.5"** — max bays in one host |
| Dell PowerEdge R730 (13th Gen), 2× E5-2660v3, 384GB DDR4 | 12,000 NOK, free shipping | Nedenes 475664047; **8-bay LFF**, 24×16GB ECC, 4× 10GbE X520 + 4× 1GbE I350 + 2× 10GbE X540-T, 2× 750W PSU, iDRAC Enterprise; **no disks**; two identical units available |
| Dell PowerEdge R740 (14th Gen), 64GB, 8×2TB SAS, H730P, 10GbE | 9,500 NOK | 476154464; disks included but 2TB; bay config unstated — verify LFF before considering |
| Dell PowerEdge R720xd (12th Gen), 2× E5-2620, 128GB RAM (12×3.5" + 2×2.5" bays) | 9,000 NOK | Åkrehamn 469749680; 16×8GB PC3L, **no disks**, 2× 750W Platinum PSU, rack rails, ships at buyer's cost, open to offers. **Decoder verdict: prior to 13th Gen → e-waste tier. Demoted.** |
| Dell PowerEdge R730 (13th Gen), 2× E5-2620v3, 64GB | 4,500 NOK | Trondheim 426464076; **2.5" SFF backplane — cannot take the 3.5" HDDs this build needs**; pickup Oslo |
| Dell PowerEdge R730 (13th Gen), 2× E5-2660v4, 256GB | 9,500 NOK | Finnsnes 460187116; no disks; **marked "considered sold"** — likely gone |
| HPE ProLiant DL380 Gen9, E5-2620v3, 64GB | 4,500 NOK | Oslo 455107692; same Haswell era as 13th Gen, 8-bay LFF, HP platform |
| Supermicro SuperServer 4U JBOD, **45× 3.5" hot-swap, SAS2 expander (2 in/2 out), 1280W redundant Platinum PSU, 7 fans** | 14,000 NOK | Bergen 421957363; chassis only, no CPU/RAM; pickup (shipping negotiable). Future daisy-chain candidate |

R730 and DL380 Gen9 parts (RAM, PERC, rails) are plentiful on finn.no, so local spares are easy. eBay UK DS4246 item pages are bot-blocked, so **shipping a ~25-30 kg 4U shelf to Norway is unconfirmed** — that is the main availability risk of the cheapest path.

Three paths to ~55 TiB usable (6×16TB RAIDZ2, disks verified at 3000-3500 NOK each on finn.no):

| Path | Hardware | Total | Import risk | Notes |
|---|---|---|---|---|
| A: shelf + existing TrueNAS | DS4246 ~6.3k landed (est) + HBA/cables ~0.7k + disks 18-21k | ~25-28k NOK | shelf freight unverified | Cheapest on paper; **dead: no free PCIe slot in the TrueNAS box** |
| B1 (selected): R730xd 12-bay as new TrueNAS host | R730xd 5k + 2nd PSU ~0.5k + 12 caddies ~1k + disks 18-21k | ~24.5-28k NOK | none | All-local, 13th Gen per the decoder, 12 LFF bays; PERC H730 flashes to IT mode; pickup Oslo |
| B2: R730 8-bay turnkey-ish | R730 12k + HBA ~0.5k + disks 18-21k | ~30.5-33.5k NOK | none | Free shipping, 384GB RAM, dual PSU, iDRAC Ent; 6 RAIDZ2 + 1 boot + 1 spare |
| B3: R730xd 12-bay premium | R730xd 13.75k + 6 caddies ~0.5k + disks 18-21k | ~32.5-35.5k NOK | none | Boot SSDs, dual 1100W, iDRAC Ent, rails included; 28C/56T; pickup Trondheim |
| C: Bergen 45-bay chassis + existing TrueNAS | Chassis 14k + HBA/cables ~0.7k + disks 18-21k | ~32.7-35.7k NOK | none | **Dead on its own: needs an HBA slot too.** Lives on as a future daisy-chain off the R730xd if 12 bays ever fill |

Decision rule:

- D1 (dead, answered 2026-09-19): the shelf needs an HBA, and the TrueNAS box has **zero free PCIe slots** — see the verified inventory below. The x16 slot holds the RTX 2070 SUPER that Ollama needs; the four x1 slots hold the ASMedia USB 3.1 controller, the ASMedia PCI bridge, the Realtek 1GbE, and the Mellanox ConnectX 10GbE. Only ~1 free SATA port remains on the Z170 board, so internal expansion is also capped.
- D2 (selected): Dell R730xd (13th Gen, Oslo, 5,000 NOK) becomes the new TrueNAS host. Found on the second sweep after the owner asked "do I have only 1?" — it was invisible in the first search pass. Cheapest live host that satisfies the decoder (13th Gen) AND the bay requirement (12 LFF: 6 RAIDZ2 + 2 pool1 + 1 boot + 3 spare). Budget 1.5k for a second PSU and caddies. The PERC H730 needs an IT-mode flash; alternative is dropping in an LSI 9300-8i. Pickup in Oslo.
- D2-alternates: B2 (Nedenes R730, 8 bays, 384GB, dual PSU, free shipping) if the Oslo unit sells first; B3 (Trondheim R730xd, 13,750) for the turnkey option.
- D3 (future option): the Bergen 45-bay chassis daisy-chains off the R730xd's HBA if the 12 bays ever fill. Not part of the first purchase.

### Verified TrueNAS host inventory (2026-09-19, via SSH)

- Gigabyte Z170-HD3P-CF, Intel i5-6600K (4C/4T, 2015), chassis "Desktop"
- 4× 8GB DDR4-2133 non-ECC = 32GB (23GB in use with Ollama + servarr)
- RTX 2070 SUPER 8GB in the x16 slot; Mellanox ConnectX 10GbE + Realtek 1GbE; ASMedia USB 3.1; ASMedia PCI bridge
- 6× SATA (Z170 AHCI): 5 occupied
- Disks (6 total): 500GB Intenso SSD (boot-pool), 3× 240GB Kingston SSDs (apps pool, RAIDZ1, 48%), 10TB IronWolf ST10000NE0008 + 14TB Exos ST14000NM001G (pool1 stripe, 86%, scrub clean 2026-09-13)
- pool1 = 2-disk non-redundant stripe, 21.8T, confirmed on the box (matches #401)


Power is a wash between A/B/C (shelf ≈ 60-100W, R730xd dual-socket ≈ 100-180W idle; at ~1 NOK/kWh that is a few hundred NOK/yr difference). The real cost drivers are chassis and disks, not power.

## Migration plan (Path B, step by step)

1. Buy the R730xd 13th Gen (5,000 NOK, Oslo 473386139, 12× LFF + 2× SFF; budget ~1k for 12 LFF caddies and ~0.5k for a second PSU), flash the PERC H730 to IT mode or add an LSI 9300-8i, and buy the 6×16TB finn.no disks (3,000-3,500 NOK each). Boot SSD comes from spares (the old box's Kingstons) or the seller's offer.
2. Install TrueNAS SCALE on a fresh boot SSD in the R730xd; HBA in IT mode so ZFS sees raw disks.
3. Create pool2 as RAIDZ2 from the 6×16TB in the R730xd. Never extend the existing stripe.
4. While the old box keeps serving, `zfs send`/`receive` pool1 datasets → pool2; verify snapshot counts and spot-check media.
5. Cutover: repoint servarr apps and Jellyfin libraries, move the apps SSDs or re-create the apps pool, update the TrueNAS exporter EndpointSlice/alerts (TruenasPool2FreeLow/Critical), point `pool-capacity-forecast.py` at pool2.
6. Keep pool1 intact on the old box for a confidence window (it is the only backup of the media meanwhile).
7. The Z170 tower keeps the RTX 2070 SUPER and runs Ollama (Q4); afterwards: sell it with the disks, or keep it as a secondary/backup node (it has 1 free SATA + M.2).
8. PBS on takaros stays untouched.

Note: keep the old box's TrueNAS updated during the confidence window so `zfs send` streams stay version-compatible.

## Open questions before buying

- Q1 (ANSWERED 2026-09-19): TrueNAS box internals verified via SSH — see "Verified TrueNAS host inventory". Zero free PCIe slots, ~1 free SATA port. The shelf+HBA path (A, C) is dead; Path B1 (R730xd as new host) is selected.
- Q2 (ANSWERED 2026-09-19): the owner has a full 20U rack with units open — the R730xd racks fine, no floor placement needed. Noise budget still applies (2U fans are loud).
- Q3: Budget ceiling. #805 froze disk spend until ~2027-02 (F3); this plan assumes that freeze is lifted or this is planning-ahead.
- Q4: Where does Ollama live after the move? The RTX 2070 SUPER stays in the old Z170 tower (it needs a GPU-power riser kit and is a poor 2U fit in a Dell 2U), so the old box keeps running as the AI node and holds pool1 as the safety-net copy, or Ollama moves to a k3s worker if one has a GPU.

## What could not be verified live

- (Resolved 2026-09-26) eBay UK used HDD prices are now verified through the eBay Browse API with the `jbod-epaflix` Production keyset; see the Disks section. The ebay MCP is enabled and reads its keys from KeePass via `.opencode/ebay-mcp.sh`.
- SFF-8088/8644 cable prices (small spend, low risk).

finn.no was initially unverifiable but is now covered: the finn MCP (CLI or MCP server) returns live Torget results with no credentials, and finn.no item pages remain directly fetchable.
