# AGENTS.md

Instructions for coding agents working in this repo.

## Project

- Goal: scripts, apps, and automations to build a new JBOD for epaflix (https://github.com/SpyrosPsarras/epaflix/).

## Source

You are creating automations to help me find the best for value items to create our own jbod for epaflix. your sources are the following but not limited to:
- ebay.uk
- finn.no

## Git workflow

- `main` is protected, so every change goes through a PR from a branch rebased on `origin/main` and merges with a merge commit (`gh pr merge --merge`). GitHub refuses a PR that is behind `main` or has `main` merged into it (required checks `test` and `no-merge-commits`, strict up-to-date mode).

## Infrastructure

- The owner has a full 20U rack with many units left open. Rack-space is not a constraint for any build in this repo; noise and power budgeting should still be stated per candidate.

## Dell PowerEdge Model Decoder

An incomplete list, you can help by *expanding it*.

### Generations; What to look for

- Anything prior to 13th Gen should be considered E-Waste or used for retrolabbing.
- **13th Gen** — Haswell/Broadwell (Xeon v3/v4). Super cheap now, still worth using.
- **14th Gen** — Skylake/Cascade Lake (AMD EPYC Naples). Affordable, Skylake super cheap, Cascade Lake is quickly getting cheap.
- **15th Gen** — Ice Lake (AMD EPYC Rome/Milan). Expensive, coming down in price, still pretty new, configs get complicated.
- **16th Gen** — Sapphire/Emerald Rapids (AMD EPYC Genoa/Bergamo/Siena). *Fairly Expensive, Slowly being cycled.*
- **17th Gen** — Sierra Forest/Granite Rapids (AMD EPYC Turin). *New, Expensive.*

### Models: what they all do and what they're best used for

X is to be replaced by the generation number, i.e. an R730 is 13th Gen. R & T indicate whether the server is a **R**ackmount or a **T**ower.

```
R2X0 - 1U 1-socket | Desktop socket. Optional hot swap HDD & PSU.
R3X0 - 1U 1-socket | Desktop socket. Small server; Good starter server.
R4X0 - 1U 2-socket | A bit longer; Most come with an 8 hot swap SFF bay config, Hot swap PSU, 2 sockets.
R5X0 - 2U 2-socket | Larger 2U server with many configuration options. Has more PCIe.
R6X0 - 1U 2-socket | Highly compute dense server. Plenty of PCIe & RAM slots.
R7X0 - 2U 2-socket | Has configuration options for both SFF & LFF configs. Plenty of PCIe & RAM capacity.
R8X0 - 2U 2/4-socket | Extreme compute; R840: extra deep 4-socket
R9X0 - 3U/4U 4-socket | Extreme compute; R940: 3U 2/4-socket, R960: 4U 2/4-socket
XD & XD2 Variants | Servers with maximized drive capacity.
XS 15th gen | Upgraded versions of lower SKUs, R750xs ≈ R550
XA Variants | GPU focused systems
XR Variants | Rugged, for extreme environments
OEMR | Version without Dell branding.
```

Any server that ends the model number with RXX15 or RXX25 is equipped with an AMD chip. **AVOID AMD VERSIONS OF 13TH GEN OR OLDER!**
