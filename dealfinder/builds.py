"""Build optimizer: qualified Machines + the Parts they lack + qualified Disks -> the cheapest Build per Machine."""
from dataclasses import dataclass

from .config import (BOOT_BAYS, CEILING_NOK, MIN_BUILD_DISKS, PENALTY_NOK, POOL_OVERHEAD, RAM_SLOTS_PER_SOCKET,
                     RAM_TARGET_GB, TARGET_TIB)
from .rules import machine_platform

TIB = 2 ** 40
HIDDEN = ("no_cpu", "no_ram", "no_heatsink", "platform", "ceiling")  # why a Machine has no shown Build
# flat CPU/RAM Penalties saved in costs before #34; a row keeps them until the next Hunt re-scores it
OLD_PENALTIES = ("no_cpu", "cpu_unknown", "ram", "ram_unknown")


def usable_tib(disk_count, capacity_tb):
    """RAIDZ2 usable TiB: (disks - 2) x size, minus pool overhead."""
    return (disk_count - 2) * capacity_tb * 1e12 / TIB * (1 - POOL_OVERHEAD)


@dataclass
class Build:
    machine: dict
    disks: list  # the Disk Listings as [(row, count used, NOK)], like the Parts
    capacity_tb: float
    usable_tib: float
    landed_nok: float
    parts: dict  # NOK per part of the Build, and the Part Listings as [(row, count used, NOK)], for the page

    @property
    def score(self):
        return self.landed_nok / self.usable_tib

    @property
    def disk_count(self):
        return sum(n for _, n, _ in self.disks)


def machine_needs(f):
    """What a Machine lacks to run, from its facts: a CPU per empty socket (the installed model when one is in),
    RAM up to RAM_TARGET_GB (top-up sticks of the stated size and speed, else a full set) and a heatsink per CPU
    without one. None when no Part fits its platform (16th Gen and newer, a desktop socket, facts saved before #31).
    """
    socket = f.get("sockets") and machine_platform(f["model"], f["amd"], f["generation"])
    if not socket:
        return None
    installed = min(f["cpu_count"] or 0, f["sockets"])  # not stated: charged like none
    ram_gb, sticks, heatsinks = f["ram_gb"] or 0, f.get("ram_sticks"), f.get("heatsinks")
    return {"platform": socket, "sockets": f["sockets"], "cpu": f["sockets"] - installed,
            "cpu_model": f["cpu_model"] if installed else None,
            "heatsink": max(0, f["sockets"] - (installed if heatsinks is None else heatsinks)),
            "ram": None if ram_gb >= RAM_TARGET_GB else {
                "gb": RAM_TARGET_GB - ram_gb if sticks else RAM_TARGET_GB, "stick_gb": sticks and sticks["gb"],
                "speed": sticks and sticks["speed"],
                # ponytail: installed sticks are RDIMM unless the text says LRDIMM, the common kind
                "type": "LRDIMM" if sticks and sticks.get("lrdimm") else "RDIMM",
                "slots": RAM_SLOTS_PER_SOCKET * f["sockets"] - (sticks["count"] if sticks else 0)}}


def _trip_place(row):
    """Pickup trips to one place are driven once, whatever is fetched there.

    ponytail: a place is the Source's location text, so two sellers in the same town share one trip.
    """
    costs = row.get("costs") or {}
    return (row["source"], row.get("location")) if costs.get("pickup_trip") else None


def _machine_cost(machine, disk_count):
    """The Machine's Landed cost with the caddy Penalty recounted for this Build: disks + boot bays."""
    costs = machine.get("costs") or {}
    penalties = dict(costs.get("penalties") or {})
    key = "caddies" if "caddies" in penalties or machine["facts"]["caddies_35"] is not None else "caddies_unknown"
    old = sum(penalties.pop(k, 0) for k in ("caddies", "caddies_unknown", *OLD_PENALTIES))
    needed = max(0, disk_count + BOOT_BAYS - (machine["facts"]["caddies_35"] or 0))
    if needed:
        penalties[key] = PENALTY_NOK["caddy"] * needed
    return float(machine["landed_nok"]) - old + penalties.get(key, 0), penalties


def _pick(rows, need, trips, unit):
    """Part Listings supplying `need` units, each the cheapest per unit still needed given the trips already driven:
    ([(row, units used, NOK)], NOK, trips), or None when too few are on sale. `unit` is the facts key holding the
    units a Listing sells as one lot ("count", "sticks"); Disks are picked by _pick_stock.

    ponytail: greedy, so a place whose trip only pays off over several Listings can be missed. One Listing that
    covers the whole need is tried too, so a pair is not bought after a cheaper-per-unit single.
    Rows carry "_nok", "_trip" and "_place", set once by rank_builds.
    """
    picks, total, left, trips = [], 0.0, list(rows), set(trips)

    def cost(r):
        return r["_nok"] - r["_trip"] if r["_place"] in trips else r["_nok"]

    def units(r):
        return r["facts"][unit]
    whole = min((r for r in rows if units(r) >= need), key=cost, default=None)
    whole = whole and ([(whole, need, cost(whole))], cost(whole), trips | {whole["_place"]})
    while need > 0:
        if not left:
            return whole
        r = min(left, key=lambda r: cost(r) / min(units(r), need))
        left.remove(r)
        picks.append((r, min(units(r), need), cost(r)))
        total += picks[-1][2]
        need -= units(r)
        trips.add(r["_place"])
    return whole if whole and whole[1] < total else (picks, total, trips)


def _pick_stock(rows, need, trips):
    """Disks: `need` units, each the cheapest next unit on sale given the trips already driven. A Listing sells up to
    "_units": the first unit at its Landed cost, each more at "_extra" (a single disk's price, VAT and its own extra
    shipping, no second trip; 0 for the rest of a lot, which the first unit paid for).
    ([(row, units used, NOK)], NOK, trips), or None when too few are on sale.

    Two answers, the cheaper wins: a greedy one, unit by unit, that drives a Pickup trip once for several Listings at
    one place; and an exact one over how many units each Listing supplies, which counts each Listing's trip on its own
    (then recounted, so its NOK is what the owner pays). ponytail: a mix that needs both a shared trip and a lot can
    be missed.
    """
    if sum(r["_units"] for r in rows) < need:
        return None

    def first(r, trips):
        return r["_nok"] - r["_trip"] if r["_place"] in trips else r["_nok"]

    def priced(counts):  # [(row, units)] -> ([(row, units, NOK)], NOK, trips), each place's trip driven once
        picks, driven = [], set(trips)
        for r, k in counts:
            picks.append((r, k, first(r, driven) + (k - 1) * r["_extra"]))
            driven.add(r["_place"])
        return picks, sum(nok for _, _, nok in picks), driven

    used = {}  # id(row) -> [row, units]
    for left in range(need, 0, -1):
        driven = set(trips) | {u[0]["_place"] for u in used.values()}

        def per_unit(r):  # the next unit of a Listing already used, else a new one's NOK per unit it could supply
            k = min(r["_units"], left)
            return r["_extra"] if id(r) in used else (first(r, driven) + (k - 1) * r["_extra"]) / k
        r = min((r for r in rows if id(r) not in used or used[id(r)][1] < r["_units"]), key=per_unit)
        used.setdefault(id(r), [r, 0])[1] += 1
    greedy = priced(used.values())
    # exact: the cheapest units per unit count; singles need no search, the cheapest n of them are best
    singles = sorted((r for r in rows if r["_units"] == 1), key=lambda r: first(r, trips))
    best = {0: []}  # units -> [(row, units)], cheapest found, from Listings of several units
    for r in (r for r in rows if r["_units"] > 1):
        for have, counts in list(best.items()):
            for k in range(1, min(r["_units"], need - have) + 1):
                if have + k not in best or priced(counts + [(r, k)])[1] < priced(best[have + k])[1]:
                    best[have + k] = counts + [(r, k)]
    exact = min((priced(counts + [(r, 1) for r in singles[:need - have]]) for have, counts in best.items()
                 if need - have <= len(singles)), key=lambda p: p[1])
    return exact if exact[1] < greedy[1] else greedy


def _complete(machine, cpus, rams, heatsinks, pick):
    """The cheapest Part Listings a Machine needs, CPUs then RAM then heatsinks, sharing Pickup trips:
    ([(row, count used, NOK)], NOK, trips), or the HIDDEN reason it cannot be completed. The Parts come grouped by
    rank_builds, and `pick` is its memoized _pick."""
    need = machine_needs(machine["facts"])
    if need is None:
        return "platform"
    ram = need["ram"] or {}
    # all CPUs of one Machine are one model; an EPYC "P" is single-socket only
    cpu_sets = [(rows, need["cpu"]) for (platform, model), rows in cpus.items()
                if platform == need["platform"] and need["cpu_model"] in (None, model)
                and not (need["sockets"] > 1 and model.startswith("EPYC") and model.endswith("P"))]
    # never RDIMM with LRDIMM; ponytail: speed is matched only for a top-up, not checked against the platform
    ram_sets = [(rows, -(-ram["gb"] // gb)) for (gb, speed, kind), rows in rams.items()
                if ram and -(-ram["gb"] // gb) <= ram["slots"] and (ram["stick_gb"] is None or (
                    (gb, kind) == (ram["stick_gb"], ram["type"]) and ram["speed"] in (None, speed)))]
    # ponytail: a Supermicro heatsink is matched by socket family only; LGA2011 narrow-ILM heatsinks do not fit a
    # square-ILM board, so a Supermicro Build can get one that does not mount. Read ILM from the title if that bites
    model = machine["facts"]["model"]
    key = f"Supermicro {need['platform'].replace('LGA2011-3', 'LGA2011')}" if model == "Supermicro" else model
    options = [("no_cpu", need["cpu"], cpu_sets, "count"), ("no_ram", ram, ram_sets, "sticks"),
               ("no_heatsink", need["heatsink"], [(heatsinks.get(key, []), need["heatsink"])], "count")]
    picked, total, trips = [], 0.0, {machine["_place"]} - {None}
    for reason, needed, sets, unit in options:
        if not needed:
            continue
        best = min((p for rows, n in sets if (p := pick(rows, n, trips, unit))),
                   key=lambda p: p[1], default=None)
        if best is None:
            return reason
        picked, total, trips = picked + best[0], total + best[1], best[2]
    return picked, total, trips


def best_build(machine, disks_by_capacity, parts, parts_nok, trips, pick):
    """Cheapest Build for one completed Machine that reaches TARGET_TIB, or None. A Disk Listing supplies its lot, or
    up to its stock (eBay reads it; every other single is one disk)."""
    max_disks = machine["facts"]["bays_35"] - BOOT_BAYS
    best = None
    for capacity, disks in disks_by_capacity.items():
        for count in range(MIN_BUILD_DISKS, min(max_disks, sum(d["_units"] for d in disks)) + 1):
            if usable_tib(count, capacity) < TARGET_TIB:
                continue
            machine_nok, penalties = _machine_cost(machine, count)
            chosen, disks_nok, _ = pick(disks, count, trips, "stock")
            total = machine_nok + parts_nok + disks_nok
            if best is None or total < best.landed_nok:
                best = Build(machine, [(d, n, round(nok, 2)) for d, n, nok in chosen], capacity,
                             usable_tib(count, capacity), round(total, 2),
                             {"machine": round(machine_nok, 2), "machine_penalties": penalties, "parts": parts,
                              "parts_nok": round(parts_nok, 2), "disks": round(disks_nok, 2)})
            break  # more disks of this capacity only cost more
    return best


def rank_builds(machines, disks, parts):
    """(the cheapest Build per Machine, {HIDDEN reason: Machines without a shown Build}, the Parts NOK or reason
    per Machine); the page sorts them."""
    by_capacity, hidden, builds, cpus, rams, heatsinks = {}, dict.fromkeys(HIDDEN, 0), [], {}, {}, {}
    completed = {}  # "source|id" -> NOK of the Parts the Machine lacks, or the HIDDEN reason it cannot be completed
    for r in (*machines, *disks, *parts):  # once per row, not per comparison in _pick
        r["_nok"], r["_trip"] = float(r["landed_nok"]), (r.get("costs") or {}).get("pickup_trip", 0)
        r["_place"] = _trip_place(r)
    for d in disks:  # a lot is bought whole: its extra disks cost nothing more. Rows saved before lots are one disk
        lot, stock = d["facts"].get("count", 1), d["facts"].get("stock", 1)
        d["_units"] = lot if lot > 1 else stock
        d["_extra"] = 0.0 if lot > 1 else float((d.get("costs") or {}).get("extra_unit", d["_nok"]))
        by_capacity.setdefault(float(d["capacity_tb"]), []).append(d)
    for r in parts:
        f = r["facts"]
        if r["kind"] == "cpu":
            cpus.setdefault((f["platform"], f["model"]), []).append(r)
        elif r["kind"] == "ram":
            rams.setdefault((f["gb_per_stick"], f["speed"], f["type"]), []).append(r)
        else:
            for model in f["fits"]:
                heatsinks.setdefault(model, []).append(r)
    memo = {}  # most Machines share no Pickup trip, so the same picks repeat: 5,572 calls, 188 distinct on 27 Sep 2026

    def pick(rows, need, trips, unit):
        key = (id(rows), need, frozenset(trips), unit)  # rows are the group lists above, alive for this call
        if key not in memo:
            memo[key] = _pick_stock(rows, need, trips) if unit == "stock" else _pick(rows, need, trips, unit)
        return memo[key]
    for m in machines:
        done = _complete(m, cpus, rams, heatsinks, pick)
        completed[f"{m['source']}|{m['source_id']}"] = done if isinstance(done, str) else round(done[1], 2)
        if isinstance(done, str):
            hidden[done] += 1
        elif (b := best_build(m, by_capacity, *done, pick)) and b.landed_nok > CEILING_NOK:
            hidden["ceiling"] += 1
        elif b:
            builds.append(b)
    return builds, hidden, completed
