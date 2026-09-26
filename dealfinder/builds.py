"""Build optimizer: qualified Machines x qualified Disks -> the cheapest Build per Machine, ranked by Score."""
from dataclasses import dataclass

from .config import BOOT_BAYS, CEILING_NOK, MIN_BUILD_DISKS, PENALTY_NOK, POOL_OVERHEAD, TARGET_TIB

TIB = 2 ** 40


def usable_tib(disk_count, capacity_tb):
    """RAIDZ2 usable TiB: (disks - 2) x size, minus pool overhead."""
    return (disk_count - 2) * capacity_tb * 1e12 / TIB * (1 - POOL_OVERHEAD)


@dataclass
class Build:
    machine: dict
    disks: list
    capacity_tb: float
    usable_tib: float
    landed_nok: float
    parts: dict  # NOK per part of the Build, for the page

    @property
    def score(self):
        return self.landed_nok / self.usable_tib


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
    old = penalties.pop("caddies", 0) + penalties.pop("caddies_unknown", 0)
    needed = max(0, disk_count + BOOT_BAYS - (machine["facts"]["caddies_35"] or 0))
    if needed:
        penalties[key] = PENALTY_NOK["caddy"] * needed
    return float(machine["landed_nok"]) - old + penalties.get(key, 0), penalties


def _pick_disks(disks, count, trips):
    """`count` disks, each the cheapest given the trips already driven; returns (disks, NOK).

    ponytail: greedy, so a place whose trip only pays off over several disks can be missed.
    """
    chosen, total, left, trips = [], 0.0, list(disks), set(trips)
    for _ in range(count):
        def cost(d):
            return float(d["landed_nok"]) - ((d.get("costs") or {}).get("pickup_trip", 0) if _trip_place(d) in trips else 0)
        d = min(left, key=cost)
        left.remove(d)
        chosen.append(d)
        total += cost(d)
        trips.add(_trip_place(d))
    return chosen, total


def best_build(machine, disks_by_capacity):
    """Cheapest Build for one Machine that reaches TARGET_TIB, or None.

    ponytail: each Disk Listing supplies one disk (the Sources do not say how many a seller has).
    """
    max_disks = machine["facts"]["bays_35"] - BOOT_BAYS
    best = None
    for capacity, disks in disks_by_capacity.items():
        for count in range(MIN_BUILD_DISKS, min(max_disks, len(disks)) + 1):
            if usable_tib(count, capacity) < TARGET_TIB:
                continue
            machine_nok, penalties = _machine_cost(machine, count)
            chosen, disks_nok = _pick_disks(disks, count, {_trip_place(machine)} - {None})
            total = machine_nok + disks_nok
            if best is None or total < best.landed_nok:
                best = Build(machine, chosen, capacity, usable_tib(count, capacity), round(total, 2),
                             {"machine": round(machine_nok, 2), "machine_penalties": penalties,
                              "disks": round(total - machine_nok, 2)})
            break  # more disks of this capacity only cost more
    return best


def rank_builds(machines, disks):
    """The cheapest Build per Machine, hiding Builds above the Ceiling; the page sorts them."""
    by_capacity = {}
    for d in disks:
        by_capacity.setdefault(float(d["capacity_tb"]), []).append(d)
    return [b for m in machines if (b := best_build(m, by_capacity)) and b.landed_nok <= CEILING_NOK]
