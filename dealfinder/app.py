"""The Deal Finder app: runs Hunts and serves the page and /metrics."""
import datetime
import html
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import (CEILING_NOK, CPU_QUERIES, DISK_MIN_NOK_PER_TB, DISK_QUERIES, HEATSINK_QUERIES, HISTORY_WEEKS,
                     HUNT_INTERVAL_S, MACHINE_QUERIES, MAX_QUERY_CHARS, PICKUP_MAX_MINUTES, PICKUP_NOK_PER_KM,
                     RAM_MIN_NOK_PER_GB, RAM_QUERIES, RAM_TARGET_GB, SOURCE_PAUSE_S, TARGET_TIB)
from .builds import HIDDEN, Build, machine_needs, rank_builds
from .costs import OsrmRouter, cost_breakdown, machine_penalties
from .rules import Unreadable, priced_per_unit, read_cpu, read_disk, read_heatsink, read_machine, read_ram, stock_in
from .store import Store, _history_key

log = logging.getLogger("dealfinder")

KINDS = ("disk", "machine", "cpu", "ram", "heatsink")

CONDITION_LABEL = {"new": "New", "refurbished": "Refurbished", "used": "Used", "for_parts": "For parts"}
FACT_LABEL = {"capacity": "capacity", "form_factor": "3.5\" or 2.5\"", "disk_class": "Disk class",
              "condition": "condition", "quantity": "how many disks the price buys", "shipping": "shipping to Norway",
              "generation": "generation", "bays_35": "3.5\" bay count", "model": "CPU model", "platform": "CPU socket",
              "gb_per_stick": "GB per stick", "ddr": "DDR generation", "type": "RDIMM or LRDIMM",
              "fits": "Machine model it fits",
              "price": "price (make an offer)", "location": "pickup place"}
COST_LABEL = {"shipping": "shipping", "shipping_estimate": "shipping (est.)", "finn_fee": "Trygg betaling (est.)",
              "pickup_trip": "pickup trip",
              "vat": "VAT", "single_psu": "2nd PSU", "no_psu": "2 PSUs", "caddies": "caddies", "raid_only": "HBA",
              "no_rails": "rails",
              "psu_unknown": "2nd PSU (not stated)", "caddies_unknown": "caddies (not stated)",
              "controller_unknown": "HBA (controller not stated)", "rails_unknown": "rails (not stated)",
              "weak_seller": "weak seller +10%", "seller_unknown": "seller rating not stated +10%"}
PART_LABEL = {"cpu": "CPU", "ram": "RAM", "heatsink": "Heatsink"}


STYLE = """<meta name="color-scheme" content="dark"><style>
:root{color-scheme:dark}
body{font-family:sans-serif;margin:2em;background:#121212;color:#e0e0e0}
a{color:#8ab4f8} a:visited{color:#c58af9} th a,th a:visited{color:#e0e0e0}
table{border-collapse:collapse;margin-bottom:2em} td,th{padding:4px 10px;border-bottom:1px solid #333;text-align:left}
input,select,button{background:#1e1e1e;color:#e0e0e0;border:1px solid #555;padding:4px 8px;border-radius:3px}
button{cursor:pointer} button:hover{background:#2a2a2a}
.fault{background:#3b1f1f;border-left:4px solid #ef5350;padding:8px 12px}
.notice{background:#1a2a3f;border-left:4px solid #64b5f6;padding:8px 12px}
.bought{background:#1b3320;border-left:4px solid #66bb6a;padding:8px 12px}
tr.gone,tr.gone a{color:#777} .note{font-size:85%} .costs{color:#9e9e9e}
.drop{color:#66bb6a;font-weight:bold} .up{color:#ef5350} .down{color:#66bb6a}
.act{white-space:nowrap} .act details{display:inline-block;position:relative}
.act summary{list-style:none;display:inline-block;cursor:pointer;border:1px solid #555;border-radius:3px;padding:4px 6px}
.act summary::-webkit-details-marker{display:none} .act details>form{position:absolute;right:0;z-index:2}
tr.deal{background:#2e2a12} .deal-note{color:#ffd54f;font-weight:bold}
th{position:sticky;top:0;z-index:1;background:#121212;box-shadow:0 1px 0 #333}
.tabs>input{position:absolute;opacity:0} .tabs>section{display:none;padding-top:1em}
.tabs>label{display:inline-block;padding:6px 14px;border:1px solid #555;border-bottom:none;border-radius:3px 3px 0 0;cursor:pointer}
.tabs>input:checked+label{background:#2a2a2a;font-weight:bold} .tabs>input:focus-visible+label{outline:2px solid #8ab4f8}
"""
STYLE += "".join(f".tabs>input:nth-of-type({n}):checked~section:nth-of-type({n})" + "{display:block}\n"
                 for n in range(1, 8))  # ponytail: CSS-only tabs, up to 7 per page; raise the range for more
STYLE += "</style>"


def _tabs(sections):
    """[(label, html)] as CSS-only tabs, the first one open."""
    heads = "".join(f'<input type="radio" name="tab" id="tab{i}"{" checked" if i == 0 else ""}>'
                    f'<label for="tab{i}">{_e(label)}</label>' for i, (label, _) in enumerate(sections))
    return f'<div class="tabs">{heads}{"".join(f"<section>{body}</section>" for _, body in sections)}</div>'


def _history_table(rows, label, unit):
    """One row per (key, Source), one column per week: 'lowest / median (Listings)'."""
    weeks = sorted({r["week"] for r in rows})
    groups = {}
    for r in rows:
        groups.setdefault((r["key"], r["source"]), {})[r["week"]] = r
    body = []
    for (key, source), cells in sorted(groups.items(), key=lambda g: (str(g[0][0]), g[0][1])):
        first, last = cells[min(cells)], cells[max(cells)]
        change = (float(last["median"]) / float(first["median"]) - 1) * 100 if len(cells) > 1 else 0
        trend = (f'<td class="{"up" if change > 0 else "down"}">{change:+.0f}%</td>' if len(cells) > 1
                 else "<td>&ndash;</td>")
        body.append(f'<tr data-history="{_e(label(key))}|{_e(source)}"><td>{_e(label(key))}</td><td>{_e(source)}</td>'
                    + "".join(f'<td>{c["low"]:,.0f} / {float(c["median"]):,.0f} <small>({c["n"]})</small></td>'
                              if (c := cells.get(w)) else "<td></td>" for w in weeks) + trend + "</tr>")
    head = "".join(f"<th>week of {w:%d %b}</th>" for w in weeks)
    return (f"<table><tr><th>{_e(unit)}</th><th>Source</th>{head}<th>Median change</th></tr>{''.join(body)}</table>"
            if body else "<p>No data yet.</p>")


def _safe_url(url):
    """Only https links reach the page; anything else (javascript:, data:) becomes inert."""
    return url if url.startswith("https://") else "#"


def _e(value):
    return html.escape("" if value is None else str(value))


def _where(row):
    minutes = (row.get("costs") or {}).get("pickup_minutes")
    drive = f" ({minutes:.0f} min)" if minutes is not None else ""
    return _e(row["source"]) + (" &middot; pickup " + _e(row["location"]) + drive if row["pickup_only"] else "")


def _breakdown(row, penalties=None):
    """'price + part + ...' under a Landed cost; the whole cost is visible, not just the total.

    `penalties` replaces the stored ones, for a Machine whose caddy Penalty a Build recounted.
    """
    c = row.get("costs") or {}
    c = {**c, "penalties": penalties} if c and penalties is not None else c
    parts = [f"{c.get('price', 0):,.0f}"]
    parts += [f"{COST_LABEL[k]} {c[k]:,.0f}" for k in ("shipping", "shipping_estimate", "finn_fee", "pickup_trip", "vat")
              if c.get(k)]
    parts += [f"{COST_LABEL.get(k, k)} {v:,.0f}" for k, v in (c.get("penalties") or {}).items()]
    return '<br><small class="costs">' + _e(" + ".join(parts)) + "</small>" if c else ""


def _row_state(row, per=None, deal=None, unit="NOK"):
    """(class/data attributes, gone, price-drop and deal notes) for one ranked row. `per` is the row's Landed NOK per
    unit and `deal` its group's deal price (Store.deal_prices), in `unit`; a live row under it is a deal."""
    if row["gone"]:
        return (' class="gone" data-gone="1"',
                f' <span class="note">gone, last seen {row["last_seen"]:%Y-%m-%d}, last seller price {float(row["price"]):,.0f} '
                f'{_e(row["currency"])}</span>')
    attrs, note = "", ""
    prev, now = row["prev_price"], row["price"]
    if prev is not None and now < prev:
        attrs, note = ' data-drop="1"', (f' <span class="drop" title="seller price dropped">&darr; seller price was '
                                         f'{float(prev):,.0f} {_e(row["currency"])}</span>')
    if deal is not None and per < deal:
        attrs += ' class="deal" data-deal="1"'
        note += (f' <span class="deal-note">That\'s a deal: under the lowest price of earlier weeks'
                 f' ({deal:,.0f} {unit})</span>')
    return attrs, note


def _deal(deals, kind, row):
    """The deal price of a ranked row's group, None when its history is too thin."""
    return deals.get((kind, float(row["capacity_tb"]) if kind == "disk" else _history_key(kind, row["facts"])[0]))


def _sources(b):
    return ", ".join(sorted({b.machine["source"], *(r["source"] for r, _, _ in b.parts["parts"]),
                             *(d["source"] for d, _, _ in b.disks)}))


BUILD_SORT = {  # column -> (header, key); every column sortable, server-side, no JavaScript
    "score": ("Score (NOK/TiB)", lambda b: b.score),
    "landed": ("Landed NOK", lambda b: b.landed_nok),
    "usable": ("Usable TiB", lambda b: -b.usable_tib),
    "machine": ("Machine", lambda b: b.machine["title"].lower()),
    "disks": ("Disks", lambda b: (b.disk_count, b.capacity_tb)),
    "sources": ("Sources", _sources),
}


def _needs(facts):
    """What a Machine lacks, for Best Machines: '2 CPU, 128 GB, 2 HS', 'nothing' or 'unsupported platform'."""
    n = machine_needs(facts)
    if n is None:
        return "unsupported platform"
    ram = n["ram"] or {}
    return ", ".join(text for text, value in ((f"{n['cpu']} CPU", n["cpu"]), (f"{ram.get('gb')} GB", ram),
                                              (f"{n['heatsink']} HS", n["heatsink"])) if value) or "nothing"


CANNOT_COMPLETE = {"no_cpu": "no CPU on sale", "no_ram": "no RAM on sale", "no_heatsink": "no heatsink on sale",
                   "platform": "unsupported platform"}


def _completed(row, parts):
    """(data attribute, Completed NOK cell) of a Machine: its Landed cost + the Parts it lacks (`parts`, NOK), or why
    the Parts cannot be bought. A Machine with no ranking yet shows '?'."""
    if not isinstance(parts, (int, float)):
        return "", _e(CANNOT_COMPLETE.get(parts, "?"))
    landed = float(row["landed_nok"])
    return (f' data-completed="{landed + parts:.2f}"',
            f'{landed + parts:,.0f}<br><small class="costs">{landed:,.0f} + parts {parts:,.0f}</small>')


def _link(row):
    return f'<a href="{_e(_safe_url(row["url"]))}">{_e(row["title"])}</a>'


def _bought_html(bought):
    if not bought:
        return ""
    b = bought["build"]
    links = "".join(f'<li>{_e(p["source"])}: <a href="{_e(_safe_url(p["url"]))}">{_e(p["title"])}</a> '
                    f'{p["landed_nok"]:,.0f} NOK</li>' for p in [b["machine"], *b.get("parts", []), *b["disks"]])
    return (f'<div class="bought" data-bought="{_e(b["machine"]["source_id"])}"><h2>Bought {_when(bought["bought"])}</h2>'
            f'<p>{_e(b["machine"]["title"])} + {sum(d.get("count", 1) for d in b["disks"])} &times; '
            f'{b["capacity_tb"]:g} TB: '
            f'{b["landed_nok"]:,.0f} NOK, {b["usable_tib"]} TiB usable, Score {b["score"]:,.0f}. '
            f'Hunting has stopped for good.</p><ul>{links}</ul></div>')


# navigator.clipboard exists only over https or localhost; elsewhere a prompt shows the id, selected, to copy by hand
COPY_JS = ("const ask = () => prompt('Copy the Build ID', this.dataset.copy); navigator.clipboard ? "
           "navigator.clipboard.writeText(this.dataset.copy).then(() => this.textContent = 'Copied', ask) : ask()")


def _build_rows(builds, sort, can_buy=True):
    rows = []
    for b in sorted(builds, key=BUILD_SORT[sort][1]):
        m = b.machine
        key = f'{_e(m["source"])}|{_e(m["source_id"])}'  # a Build's id: one Build per Machine, as /buy reads it
        sources = _sources(b)
        items = [f"<li>Machine {_link(m)}: {b.parts['machine']:,.0f} NOK{_breakdown(m, b.parts['machine_penalties'])}</li>"]
        items += [f'<li data-part="{_e(r["source_id"])}" data-count="{n}" data-nok="{nok:.2f}">{PART_LABEL[r["kind"]]} '
                  f"{_link(r)}: {n} used, {nok:,.0f} NOK{_breakdown(r)}</li>" for r, n, nok in b.parts["parts"]]
        items += [f'<li data-disk="{_e(d["source_id"])}" data-count="{n}" data-nok="{nok:.2f}">Disk {_link(d)}: '
                  f"{n} used, {nok:,.0f} NOK{_breakdown(d)}"
                  + (f'<br><small class="costs">each after the first {extra:,.0f}</small>'
                     if n > 1 and (extra := (d["costs"] or {}).get("extra_unit")) else "") + "</li>"
                  for d, n, nok in b.disks]
        rows.append(
            f'<tr data-build="{_e(m["source_id"])}" data-score="{b.score:.2f}" data-landed="{b.landed_nok:.2f}">'
            f"<td>{b.score:,.0f}</td><td>{b.landed_nok:,.0f}</td><td>{b.usable_tib:.1f}</td>"
            f"<td>{_link(m)}</td><td>{b.disk_count} &times; {b.capacity_tb:g} TB</td><td>{_e(sources)}</td>"
            + f'<td class="act"><button type="button" data-copy="{key}" onclick="{COPY_JS}">Copy ID</button>'
            + (f'<details><summary title="More">&#9662;</summary><form method="post" action="/buy" onsubmit="return confirm('
               f'\'Record this Build as bought and stop hunting for good?\')"><input type="hidden" name="machine" '
               f'value="{key}"><button>Mark as bought</button></form></details>' if can_buy else "")
            + "</td>"
            + f"<td><details><summary>show</summary><ul>{''.join(items)}</ul>"
            f"<p>Total {b.landed_nok:,.0f} NOK = Machine {b.parts['machine']:,.0f} + Parts {b.parts['parts_nok']:,.0f}"
            f" + Disks {b.parts['disks']:,.0f}"
            f" (a shared pickup place is driven once)</p></details></td></tr>")
    return "".join(rows)


def _per_unit(row):
    """Landed NOK per TB for a Disk, per CPU or heatsink for those Listings, per GB for RAM; None for a Machine or
    without a Landed cost."""
    f = row["facts"] or {}
    unit = (row["capacity_tb"] * f.get("count", 1) if row["capacity_tb"] else f.get("count")
            or (f.get("gb_per_stick") or 0) * f.get("sticks", 0))
    return row["landed_nok"] / unit if row["landed_nok"] and unit else None


def clean_query(query, kind):
    """(query, kind) as typed by the owner, normalised; None when unusable."""
    query = " ".join((query or "").replace("\x00", " ").lower().split())[:MAX_QUERY_CHARS]
    return (query, kind) if query and kind in KINDS else None


def _yes_no(value):
    return {True: "yes", False: "no"}.get(value, "?")


def _when(ts):
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "never"


def next_hunt_delay(last_started, now, interval=HUNT_INTERVAL_S):
    """Seconds until the next scheduled Hunt: due one interval after the last one started, now if overdue."""
    if last_started is None:
        return 0.0
    return max(0.0, (last_started - now).total_seconds() + interval)


def source_faults(detail):
    """(source, reason) for every Source that failed or found no Listings in one Hunt's detail."""
    faults = []
    for name, result in sorted((detail or {}).items()):
        if not result.get("ok"):
            faults.append((name, "failed: " + (result.get("error") or "unknown error")))
        elif not sum(result.get(k, 0) for k in KINDS):
            faults.append((name, "returned no Listings for any Tracked query"))
    return faults


class App:
    def __init__(self, db_uri, sources, fx, disk_queries=None, machine_queries=None, cpu_queries=None,
                 ram_queries=None, heatsink_queries=None, pause=SOURCE_PAUSE_S, router=None):
        self.store = Store(db_uri)
        self.store.migrate()
        self.router = router or OsrmRouter(self.store)
        self.sources, self.fx, self.pause = sources, fx, pause
        for source in sources:  # eBay stock read by an earlier pod counts against the same daily quota
            if hasattr(source, "seed_stock"):
                source.seed_stock(self.store.stock_read(source.name))
        disk_queries, machine_queries = disk_queries or DISK_QUERIES, machine_queries or MACHINE_QUERIES
        cpu_queries, ram_queries = cpu_queries or CPU_QUERIES, ram_queries or RAM_QUERIES
        heatsink_queries = heatsink_queries or HEATSINK_QUERIES
        self.store.seed_tracked([(q, "disk", src) for src, qs in disk_queries.items() for q in qs]
                                + [(q, "machine", "") for q in machine_queries]
                                + [(q, "cpu", src) for src, qs in cpu_queries.items() for q in qs]
                                + [(q, "ram", src) for src, qs in ram_queries.items() for q in qs]
                                + [(q, "heatsink", src) for src, qs in heatsink_queries.items() for q in qs])
        self._hunt_lock = threading.Lock()  # one Hunt at a time, scheduled or by hand
        self.ranking_error = None  # why the last ranking in this process failed, shown on the page and /metrics
        last = self.store.last_hunt()
        try:  # re-rank the stored Listings so a deploy's rules show now, not after the next Hunt
            if last:
                self.rank(last["id"])
        except Exception:  # a start-up must not fail on it; the page shows the fault
            log.exception("ranking at start-up failed")

    def start_hunt(self):
        """Start a Hunt in the background. False when one is already running or the Build is bought."""
        if self.store.bought() or not self._hunt_lock.acquire(blocking=False):
            return False

        def run():
            try:
                self.hunt()
            except Exception:  # a crashed Hunt must not kill the scheduler or leave the lock held
                log.exception("hunt crashed")
            finally:
                self._hunt_lock.release()

        threading.Thread(target=run, name="hunt", daemon=True).start()
        return True

    def hunt_running(self):
        return self._hunt_lock.locked()

    def run_scheduler(self, stop=None, interval=HUNT_INTERVAL_S, retry=60):
        """Start a Hunt whenever one is due, forever (or until `stop` is set). A restart never starts one: a Hunt
        overdue at start-up waits one interval from then, and Hunt now starts one sooner."""
        stop = stop or threading.Event()
        boot = datetime.datetime.now(datetime.timezone.utc)
        while not stop.is_set():
            try:
                now, last = datetime.datetime.now(datetime.timezone.utc), self.store.last_started()
                if last is None or (boot - last).total_seconds() >= interval:
                    last = boot
                if stop.wait(next_hunt_delay(last, now, interval)):
                    break
                self.start_hunt()
            except Exception:  # e.g. Postgres failover: the scheduler must outlive it, or Hunts stop silently
                log.exception("scheduler pass failed; retrying")
            stop.wait(retry)  # let the Hunt record its start before the next delay is computed

    def hunt(self):
        """One pass over every Source. A failing Source is recorded and does not stop the others."""
        if self.store.bought():  # checked here too, so no path (scheduler, Hunt now, CLI) calls a Source again
            log.info("Build bought: hunting has stopped")
            return
        hunt_id = self.store.start_hunt()
        detail, ok, tracked = {}, True, self.store.tracked()
        for source in self.sources:
            if self.store.bought():  # bought while this Hunt runs: stop before the next Source
                break
            counts = dict.fromkeys(KINDS, 0)
            try:
                work = [(r["query"], r["kind"]) for r in tracked if r["source"] in ("", source.name)]
                for query, kind in work:
                    for listing in source.search(query, kind):
                        counts[kind] += self._record(source, listing, kind, hunt_id)
                    time.sleep(self.pause)
                detail[source.name] = {"ok": True, **counts}
            except Exception as exc:  # a Source fault must not end the Hunt
                log.exception("source %s failed", source.name)
                detail[source.name] = {"ok": False, "error": str(exc)[:300], **counts}
                ok = False
        self.store.finish_hunt(hunt_id, ok, detail)
        log.info("hunt %s done ok=%s %s", hunt_id, ok, detail)
        # the Builds the page shows, and the best one for the price history; a failure must not fail the Hunt.
        # ponytail: ranked after finish_hunt because build_parts counts finished Hunts only, so until rank() returns
        # the page shows the previous ranking: ~1 s in production, or until a ranking works if it fails (the page
        # and dealfinder_ranking_failed say so). Publish both in one transaction, with a savepoint around the
        # ranking, if that gap starts to matter
        try:
            builds, hidden = self.rank(hunt_id)
            b = min(builds, key=lambda b: b.score, default=None)
            self.store.set_best_build(hunt_id, b and {
                "score": round(b.score, 2), "landed_nok": b.landed_nok, "usable_tib": round(b.usable_tib, 2),
                "machine": b.machine["title"], "url": b.machine["url"],
                "disks": f"{b.disk_count} x {b.capacity_tb:g} TB"}, hidden)
        except Exception:
            log.exception("ranking the Builds of hunt %s failed", hunt_id)

    def rank(self, hunt_id):
        """Rank the Builds from the live Listings and store the shown ones for the page: (builds, hidden counts).
        Listings change only in a Hunt, so this runs once per Hunt, and at start-up for new rules."""
        start = time.monotonic()
        try:
            builds, hidden, completed = rank_builds(*self.store.build_parts())
            self.store.set_ranking(hunt_id, [asdict(b) for b in builds], completed)
        except Exception as exc:  # the page keeps the last ranking and says so, until a ranking succeeds
            self.ranking_error = f"hunt {hunt_id}: {exc}"[:300]
            raise
        self.ranking_error = None
        log.info("ranked %d Builds for hunt %s in %.1f s", len(builds), hunt_id, time.monotonic() - start)
        return builds, hidden

    def builds(self):
        """The Builds of the last ranking, as stored; the page and Mark as bought read these, never re-rank."""
        return [Build(**b) for b in self.store.ranking()]

    def _score(self, source, listing, kind):
        """The rules' verdict on one Listing: None when it is not of that kind at all, else a dict with
        facts, missing, qualifies, capacity_tb, landed_nok and costs. Hunts save it; Search only shows it."""
        if kind == "disk":
            facts = read_disk(listing.title, listing.condition)
        elif kind == "cpu":
            facts = read_cpu(listing.title, listing.condition)
        elif kind == "ram":
            facts = read_ram(listing.title, listing.condition)
        elif kind == "heatsink":
            facts = read_heatsink(listing.title, listing.condition)
        else:
            facts = read_machine(listing.title, listing.description, listing.condition)
        if facts is None:
            return None
        if listing.price <= 0 and not isinstance(facts, Unreadable) and not facts.qualifies:
            # "make an offer", but a known fact already rules it out (DDR3 RAM, a 12th Gen server): rejected
            return {"facts": asdict(facts), "missing": [], "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        if listing.price <= 0:  # "make an offer": no price to rank
            facts = Unreadable((facts.missing if isinstance(facts, Unreadable) else []) + ["price"])
        if isinstance(facts, Unreadable):
            return {"facts": None, "missing": facts.missing, "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        facts_json = asdict(facts)
        # the readers see the title only; a price per unit stated in the description is applied here.
        # ponytail: a per-unit Part Listing supplies one unit, even when the seller has several ("Bare 4 igjen")
        unit = {"disk": "count", "cpu": "count", "ram": "sticks", "heatsink": "count"}.get(kind)
        per_unit = unit and priced_per_unit(listing.description, listing.price)
        if per_unit:
            facts_json[unit] = 1
        penalties = machine_penalties(facts_json) if kind == "machine" and facts.bays_35 is not None else None
        costs, problem = cost_breakdown(listing, self.fx, source.foreign, self.router, kind, penalties)
        # unknown shipping or place is a missing fact; a pickup beyond the limit is a disqualifier (rejected)
        landed = None if problem else costs["total"]
        if kind == "disk":
            # a lot ("4x 16TB") is bought whole at its price. A single disk sells up to its stock: eBay's, or what a
            # text priced per disk states ("Selger 4 stk. Pris per stk"). Each unit after the first pays the price,
            # VAT and its extra shipping (unknown: the full shipping again, never a fake bargain), no second trip.
            # ponytail: a fixed floor, as for RAM: a lot under DISK_MIN_NOK_PER_TB is read as one disk
            lot_tb = (facts.capacity_tb or 0) * facts_json["count"]
            if facts_json["count"] > 1 and (not lot_tb or costs["price"] / lot_tb < DISK_MIN_NOK_PER_TB):
                facts_json["count"] = 1
            stock = max(stock_in(listing.description), facts.count) if per_unit else listing.stock
            facts_json["stock"] = stock if facts_json["count"] == 1 else 1
            if listing.stock_read:  # kept so a restart does not read eBay stock again (EbaySource.seed_stock)
                facts_json["stock_read"], facts_json["extra_shipping"] = listing.stock_read, listing.extra_shipping
            if landed and facts_json["stock"] > 1:
                more = replace(listing, shipping=listing.shipping if listing.extra_shipping is None
                               else listing.extra_shipping, lat=None, lon=None, pickup_only=False)
                costs["extra_unit"] = cost_breakdown(more, self.fx, source.foreign, self.router, kind)[0]["total"]
        # ponytail: a fixed floor; a real bulk lot under RAM_MIN_NOK_PER_GB is read as one stick and ranks too dear.
        # Lower the floor as used DDR4 prices fall. The seller's price, not Landed: a pickup trip lifts a per-stick
        # price over the floor
        if (kind == "ram" and landed and facts.gb_per_stick and facts.sticks > 1
                and costs["price"] / (facts.gb_per_stick * facts.sticks) < RAM_MIN_NOK_PER_GB):
            facts_json["sticks"] = 1
        return {"facts": facts_json, "missing": [problem] if problem in ("shipping", "location") else [],
                "qualifies": facts.qualifies and landed is not None, "landed_nok": landed, "costs": costs,
                "capacity_tb": facts.capacity_tb if kind == "disk" else None}

    def _record(self, source, listing, kind, hunt_id):
        s = self._score(source, listing, kind)
        if s is None:
            return 0
        self.store.save_listing(listing, kind, s["facts"], s["missing"], s["qualifies"], s["capacity_tb"],
                                s["landed_nok"], hunt_id, s["costs"])
        return 1

    def mark_bought(self, machine_key):
        """Record the current Build for one Machine ("source|source_id") as bought. False when it is not shown."""
        for b in self.builds():
            if f'{b.machine["source"]}|{b.machine["source_id"]}' == machine_key:
                def part(row):
                    return {k: row.get(k) for k in ("source", "source_id", "title", "url", "location")} | {
                        "landed_nok": float(row["landed_nok"])}
                if not self.store.record_purchase({
                        "machine": part(b.machine), "capacity_tb": b.capacity_tb,
                        "disks": [part(d) | {"count": n, "landed_nok": nok} for d, n, nok in b.disks],
                        "parts": [part(r) | {"kind": r["kind"], "count": n} for r, n, _ in b.parts["parts"]],
                        "usable_tib": round(b.usable_tib, 2), "landed_nok": b.landed_nok, "score": round(b.score, 2)}):
                    return False
                log.info("Build bought: %s, %s x %g TB, %.0f NOK", b.machine["title"], b.disk_count, b.capacity_tb,
                         b.landed_nok)
                return True
        return False

    def search(self, query, kind):
        """Run one query on every Source now and score the results; nothing is saved.
        Returns (rows, faults): rows are dicts of the Listing fields plus the score, best first."""
        rows, faults = [], []
        for source in self.sources:
            try:
                for listing in source.search(query, kind):
                    s = self._score(source, listing, kind)
                    if s is not None:
                        rows.append({**asdict(listing), **s})
            except Exception as exc:  # one failing Source must not hide the others' results
                log.exception("search on %s failed", source.name)
                faults.append((source.name, str(exc)[:200]))

        def rank(r):
            return (not r["qualifies"], r["landed_nok"] is None, _per_unit(r) or r["landed_nok"] or 0)
        return sorted(rows, key=rank), faults

    def search_page(self, query, kind):
        rows, faults = self.search(query, kind)
        body = []
        for r in rows:
            if r["qualifies"]:
                verdict = "Qualifies"
            elif r["missing"]:
                verdict = "Could not read: " + ", ".join(FACT_LABEL.get(m, m) for m in r["missing"])
            else:
                verdict = "Rejected by the rules"
            landed, per = r["landed_nok"], _per_unit(r)
            body.append(
                f'<tr data-result="{_e(r["source_id"])}" data-qualifies="{int(r["qualifies"])}"><td>{_e(r["source"])}</td>'
                f'<td><a href="{_e(_safe_url(r["url"]))}">{_e(r["title"])}</a>{_breakdown(r)}</td><td>{_e(verdict)}</td>'
                f'<td>{"" if landed is None else f"{landed:,.0f}"}</td>'
                f'<td>{"" if per is None else f"{per:,.0f}"}</td></tr>')
        body = "".join(body)
        unit = {"cpu": "CPU", "ram": "GB", "heatsink": "heatsink"}.get(kind, "TB")
        fault = "".join(f'<p class="fault" data-fault="{_e(n)}">Source fault: <b>{_e(n)}</b> {_e(m)}</p>' for n, m in faults)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Search: {_e(query)}</title>
{STYLE}</head><body>
<p><a href="/">&larr; Deal Finder</a></p><h1>Search: {_e(query)} ({_e(kind)}s)</h1>{fault}
<form method="post" action="/track"><input type="hidden" name="q" value="{_e(query)}">
<input type="hidden" name="kind" value="{_e(kind)}"><button>Track this query</button> (every Hunt will run it)</form>
<p>{len(rows)} results, {sum(r["qualifies"] for r in rows)} qualify. Scored with the same rules as a Hunt; nothing is saved.</p>
<table><tr><th>Source</th><th>Listing</th><th>Verdict</th><th>Landed NOK</th><th>NOK per {_e(unit)}</th></tr>{body}</table>
</body></html>"""

    def history_page(self):
        series, builds = self.store.history()
        build_rows = "".join(
            f'<tr data-best-week="{r["week"]}"><td>week of {r["week"]:%d %b}</td><td>{b["score"]:,.0f}</td>'
            f'<td>{b["landed_nok"]:,.0f}</td><td><a href="{_e(_safe_url(b["url"]))}">{_e(b["machine"])}</a></td>'
            f'<td>{_e(b["disks"])}</td></tr>'
            for r in builds for b in [r["best_build"]])
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder: price history</title>
{STYLE}</head><body><p><a href="/">&larr; Deal Finder</a></p><h1>Price history</h1>
<p>Qualifying Listings only, by Landed cost (price + shipping or pickup trip + VAT + Penalties). Each cell is
lowest / median that week, with the number of Listings; each Listing counts once a week, at its lowest price.
History starts with this version (27 Sep 2026): earlier Hunts stored no Landed cost, so month-to-month comparison
needs a few weeks of data. The last {HISTORY_WEEKS} weeks are shown.</p>
{_tabs([
    ("Best Build per week", f"<table><tr><th>Week</th><th>Score (NOK/TiB)</th><th>Landed NOK</th><th>Machine</th>"
                            f"<th>Disks</th></tr>{build_rows}</table>" if build_rows else "<p>No data yet.</p>"),
    ("Disks, NOK per TB", _history_table(series["disk"], lambda k: f"{k:g} TB", "Capacity")),
    ("Machines, NOK", _history_table(series["machine"], str, "Model")),
    ("CPUs, NOK per CPU", _history_table(series["cpu"], str, "Model")),
    ("RAM, NOK per GB", _history_table(series["ram"], str, "RAM")),
    ("Heatsinks, NOK per heatsink", _history_table(series["heatsink"], str, "Fits"))])}
</body></html>"""

    def page(self, notice=None, sort="score"):
        sort = sort if sort in BUILD_SORT else "score"
        builds = self.builds()
        build_head = "".join(f'<th><a href="/?sort={k}">{_e(label)}</a></th>' for k, (label, _) in BUILD_SORT.items())
        last = self.store.last_hunt()
        # cheapest completed first (Landed + the Parts it lacks); a Machine that cannot be completed, or has no ranking
        # yet, goes after the rest by Landed cost. ponytail: sorted here, all Machines, no SQL limit; fine at hundreds
        parts = self.store.ranking_machines()

        def machine_parts(r):
            return parts.get(f"{r['source']}|{r['source_id']}")

        def completed_first(r):
            p = machine_parts(r)
            done = isinstance(p, (int, float))
            return r["gone"], not done, float(r["landed_nok"]) + (p if done else 0)
        machines = sorted(self.store.best_listings("machine", None), key=completed_first)[:50]
        disks, unreadable = self.store.best_disks(), self.store.unreadable()
        cpus, rams = self.store.best_listings("cpu"), self.store.best_listings("ram")
        heatsinks = self.store.best_listings("heatsink")
        deals = self.store.deal_prices()

        def state(kind, r, per):
            unit = {"disk": "NOK per TB", "cpu": "NOK per CPU", "ram": "NOK per GB", "heatsink": "NOK per heatsink"}
            return _row_state(r, per, _deal(deals, kind, r), unit.get(kind, "NOK"))
        disk_rows = "".join(
            '<tr data-listing="{id}" data-nok-per-tb="{npt:.2f}"{attrs}><td><a href="{url}">{title}</a>{note}</td>'
            '<td>{lot}{cap:g} TB</td><td>{cond}</td><td>{where}</td><td>{landed:,.0f}</td><td>{npt:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), npt=float(r["nok_per_tb"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                cap=float(r["capacity_tb"]), lot=f'{r["count"]} &times; ' if (r["count"] or 1) > 1 else "",
                cond=_e(CONDITION_LABEL.get(r["condition"], r["condition"] or "?")),
                where=_where(r), landed=float(r["landed_nok"]), attrs=st[0], note=st[1] + _breakdown(r))
            for r in disks for st in [state("disk", r, float(r["nok_per_tb"]))])
        machine_rows = "".join(
            '<tr data-machine="{id}" data-landed="{landed:.2f}"{done[0]}{attrs}><td><a href="{url}">{title}</a>{note}</td>'
            '<td>{model}</td><td>{gen}th</td><td>{bays}</td><td>{ram}</td><td>{psu}</td><td>{caddies}</td><td>{ctrl}</td>'
            '<td>{rails}</td><td>{needs}</td><td>{where}</td><td>{landed:,.0f}</td><td>{done[1]}</td></tr>'.format(
                id=_e(r["source_id"]), landed=float(r["landed_nok"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                model=_e(f["model"]), gen=_e(f["generation"]), bays=_e(f["bays_35"]),
                ram=_e("?" if f["ram_gb"] is None else f"{f['ram_gb']} GB"),
                psu=_e("?" if f["psu_count"] is None else f["psu_count"]),
                caddies=_e("?" if f["caddies_35"] is None else f["caddies_35"]), ctrl=_e(f["controller"] or "?"),
                rails=_yes_no(f["rails"]), needs=_e(_needs(f)), where=_where(r), attrs=st[0], note=st[1] + _breakdown(r),
                done=_completed(r, machine_parts(r)))
            for r in machines for f in [r["facts"]] for st in [state("machine", r, float(r["landed_nok"]))])
        cpu_rows = "".join(
            '<tr data-cpu="{id}" data-nok-per-cpu="{per:.2f}"{attrs}><td>{link}{note}</td><td>{platform}</td>'
            '<td>{model}</td><td>{count}</td><td>{where}</td><td>{landed:,.0f}</td><td>{per:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), per=float(r["landed_nok"]) / f["count"], link=_link(r), platform=_e(f["platform"]),
                model=_e(f["model"]), count=_e(f["count"]), where=_where(r), landed=float(r["landed_nok"]),
                attrs=st[0], note=st[1] + _breakdown(r))
            for r in cpus for f in [r["facts"]] for st in [state("cpu", r, float(r["landed_nok"]) / f["count"])])
        ram_rows = "".join(
            '<tr data-ram="{id}" data-nok-per-gb="{per:.2f}"{attrs}><td>{link}{note}</td><td>{type}</td>'
            '<td>{sticks} &times; {gb} GB</td><td>{speed}</td><td>{where}</td><td>{landed:,.0f}</td><td>{per:,.0f}</td>'
            '</tr>'.format(
                id=_e(r["source_id"]), per=float(r["landed_nok"]) / (f["gb_per_stick"] * f["sticks"]), link=_link(r),
                type=_e(f["type"]), sticks=_e(f["sticks"]), gb=_e(f["gb_per_stick"]),
                speed=_e(f"{f['speed']} MT/s" if f["speed"] else "?"),
                where=_where(r), landed=float(r["landed_nok"]), attrs=st[0], note=st[1] + _breakdown(r))
            for r in rams for f in [r["facts"]]
            for st in [state("ram", r, float(r["landed_nok"]) / (f["gb_per_stick"] * f["sticks"]))])
        heatsink_rows = "".join(
            '<tr data-heatsink="{id}" data-nok-per-heatsink="{per:.2f}"{attrs}><td>{link}{note}</td><td>{fits}</td>'
            '<td>{count}</td><td>{where}</td><td>{landed:,.0f}</td><td>{per:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), per=float(r["landed_nok"]) / f["count"], link=_link(r),
                fits=_e(", ".join(f["fits"])), count=_e(f["count"]), where=_where(r), landed=float(r["landed_nok"]),
                attrs=st[0], note=st[1] + _breakdown(r))
            for r in heatsinks for f in [r["facts"]] for st in [state("heatsink", r, float(r["landed_nok"]) / f["count"])])
        unreadable_rows = "".join(
            '<tr data-unreadable="{id}" data-missing="{missing}"><td>{kind}</td><td>{src}</td>'
            '<td><a href="{url}">{title}</a></td><td>{labels}</td></tr>'.format(
                id=_e(r["source_id"]), missing=_e(",".join(r["missing"])), kind=_e(r["kind"]), src=_e(r["source"]),
                url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                labels=_e(", ".join(FACT_LABEL.get(m, m) for m in r["missing"])))
            for r in unreadable)
        faults = source_faults(last["detail"]) if last else []
        success = self.store.source_last_success()
        banner = "".join(
            f'<p class="fault" data-fault="{_e(name)}">Source fault: <b>{_e(name)}</b> {_e(reason)}. '
            + (f"Its Listings below are from its last successful Hunt ({_when(success[name])}).</p>"
               if name in success else "It has no Listings yet.</p>")
            for name, reason in faults)
        if self.ranking_error:
            banner += (f'<p class="fault" data-fault="ranking">Ranking the Builds failed ({_e(self.ranking_error)}). '
                       "The Builds below are from the last ranking that worked.</p>")
        notices = {"started": "Hunt started. Refresh in a few minutes.",
                   "busy": "A Hunt is already running; this request was ignored.",
                   "tracked": "Query tracked. Every Hunt runs it from now on.",
                   "bought": "This Build is bought; hunting has stopped for good.",
                   "gone": "That Build is no longer shown; nothing was recorded."}
        bought = self.store.bought()
        tracked = self.store.tracked()
        tracked_list = {kind: " &middot; ".join(
            f'<span data-tracked="{_e(kind)}:{_e(r["query"])}">{_e(r["query"])}'
            + (f' <small>({_e(r["source"])} only)</small>' if r["source"] else "") + "</span>"
            for r in tracked if r["kind"] == kind) for kind in KINDS}
        note = f'<p class="notice" data-notice="{_e(notice)}">{_e(notices[notice])}</p>' if notice in notices else ""
        running = '<p class="notice">A Hunt is running now.</p>' if self.hunt_running() else ""
        took = f" (took {(last['finished'] - last['started']).total_seconds():.0f} s)" if last else ""
        per_source = " &middot; ".join(f"{_e(s.name)}: last successful Hunt {_when(success.get(s.name))}"
                                       for s in self.sources)
        tabs = _tabs([
            ("Builds", f"<p>One Machine plus the CPUs, RAM (to {RAM_TARGET_GB} GB) and heatsinks it lacks, plus same-size"
                       f" Disks reaching {TARGET_TIB} TiB usable in RAIDZ2; the cheapest per Machine. Builds over"
                       f" {CEILING_NOK:,} NOK, or missing a Part no Listing on sale supplies, are hidden."
                       f" Lower Score is better.</p>"
                       f"<table><tr>{build_head}<th></th><th>Details</th></tr>{_build_rows(builds, sort, bought is None)}</table>"),
            ("Best Disks", "<table><tr><th>Disk</th><th>Capacity</th><th>Condition</th><th>Where</th>"
                           f"<th>Landed NOK</th><th>NOK per TB</th></tr>{disk_rows}</table>"),
            ("Best Machines", "<p>Cheapest Completed NOK first: Landed NOK plus the cheapest CPUs, RAM (to"
                              f" {RAM_TARGET_GB} GB) and heatsinks on sale that the Machine needs. Machines whose Parts"
                              " cannot be bought come last.</p>"
                              '<table><tr><th>Machine</th><th>Model</th><th>Gen</th><th>3.5" bays</th><th>RAM</th>'
                              '<th>PSUs</th><th>3.5" caddies</th><th>Controller</th><th>Rails</th><th>Needs</th>'
                              '<th>Where</th>'
                              f"<th>Landed NOK</th><th>Completed NOK</th></tr>{machine_rows}</table>"),
            ("Best CPUs", "<table><tr><th>CPU</th><th>Platform</th><th>Model</th><th>Count</th><th>Where</th>"
                          f"<th>Landed NOK</th><th>NOK per CPU</th></tr>{cpu_rows}</table>"),
            ("Best RAM", "<table><tr><th>RAM</th><th>Type</th><th>Size</th><th>Speed</th><th>Where</th>"
                         f"<th>Landed NOK</th><th>NOK per GB</th></tr>{ram_rows}</table>"),
            ("Heatsinks", "<table><tr><th>Heatsink</th><th>Fits</th><th>Count</th><th>Where</th><th>Landed NOK</th>"
                          f"<th>NOK per heatsink</th></tr>{heatsink_rows}</table>"),
            ("Could not read", "<table><tr><th>Kind</th><th>Source</th><th>Listing</th><th>Missing</th></tr>"
                               f"{unreadable_rows}</table>")])
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder</title>
{STYLE}</head><body>
<h1>Deal Finder</h1>{banner}{note}{running}
<p>Last Hunt: {_when(last["finished"] if last else None)}{took}. Hunts run every {HUNT_INTERVAL_S // 3600} hours.
{"" if bought else '<form method="post" action="/hunt" style="display:inline"><button>Hunt now</button></form>'}</p>
{_bought_html(bought)}
<p>{per_source} &middot; <a href="/history">Price history</a></p>
<form method="get" action="/search"><input name="q" size="30" placeholder="e.g. exos x20 or r740xd" required
maxlength="{MAX_QUERY_CHARS}"> <select name="kind"><option value="disk">Disks</option><option value="machine">Machines</option>
<option value="cpu">CPUs</option><option value="ram">RAM</option>
<option value="heatsink">Heatsinks</option></select> <button>Search every Source now</button></form>
<details><summary>{len(tracked)} Tracked queries run by every Hunt</summary>
<p><b>Disks:</b> {tracked_list["disk"]}</p><p><b>Machines:</b> {tracked_list["machine"]}</p>
<p><b>CPUs:</b> {tracked_list["cpu"]}</p><p><b>RAM:</b> {tracked_list["ram"]}</p>
<p><b>Heatsinks:</b> {tracked_list["heatsink"]}</p></details>
<p>Landed cost = price + shipping or pickup trip from Sandefjord ({PICKUP_NOK_PER_KM} NOK/km, max {PICKUP_MAX_MINUTES} min one way) + finn.no Trygg betaling when shipped + import VAT + Penalties (unknown PSU, caddies, controller or rails are charged).</p>
{tabs}
</body></html>"""

    def metrics(self):
        last = self.store.last_hunt()
        detail = (last or {}).get("detail") or {}
        success = self.store.source_last_success()
        lines = ["# TYPE dealfinder_listings gauge"]
        for r in self.store.counts():
            lines.append(f'dealfinder_listings{{source="{r["source"]}",kind="{r["kind"]}",state="{r["state"]}"}} {r["n"]}')
        lines.append("# TYPE dealfinder_hunt_duration_seconds gauge")
        lines.append(f"dealfinder_hunt_duration_seconds {(last['finished'] - last['started']).total_seconds() if last else 0}")
        lines.append("# TYPE dealfinder_hunt_last_success_timestamp_seconds gauge")  # kept from ticket #2
        ok = max((t for t in success.values() if t), default=None)
        lines.append(f"dealfinder_hunt_last_success_timestamp_seconds {ok.timestamp() if ok else 0}")
        best = self.store.latest_best_build() or {}
        lines.append("# TYPE dealfinder_best_build_score gauge")  # NOK per usable TiB of the latest Hunt's best Build
        lines.append(f"dealfinder_best_build_score {best.get('score', 0)}")
        lines.append("# TYPE dealfinder_best_build_landed_nok gauge")
        lines.append(f"dealfinder_best_build_landed_nok {best.get('landed_nok', 0)}")
        lines.append("# TYPE dealfinder_builds_hidden gauge")  # Machines without a shown Build, per reason, last Hunt
        hidden = self.store.latest_builds_hidden() or dict.fromkeys(HIDDEN, 0)
        lines += [f'dealfinder_builds_hidden{{reason="{k}"}} {n}' for k, n in hidden.items()]
        lines.append("# TYPE dealfinder_bought gauge")  # 1 once a Build is bought and hunting has stopped
        lines.append(f"dealfinder_bought {int(self.store.bought() is not None)}")
        lines.append("# TYPE dealfinder_ranking_failed gauge")  # 1 while the page shows Builds from an older ranking
        lines.append(f"dealfinder_ranking_failed {int(self.ranking_error is not None)}")
        lines.append("# TYPE dealfinder_hunt_running gauge")
        lines.append(f"dealfinder_hunt_running {int(self.hunt_running())}")
        faulty = {name for name, _ in source_faults(detail)}
        lines += ["# TYPE dealfinder_source_up gauge", "# TYPE dealfinder_source_listings gauge",
                  "# TYPE dealfinder_source_last_success_timestamp_seconds gauge"]
        for s in self.sources:
            result = detail.get(s.name, {})
            lines.append(f'dealfinder_source_up{{source="{s.name}"}} {int(s.name in detail and s.name not in faulty)}')
            lines.append(f'dealfinder_source_listings{{source="{s.name}"}} {sum(result.get(k, 0) for k in KINDS)}')
            ts = success.get(s.name)
            lines.append(f'dealfinder_source_last_success_timestamp_seconds{{source="{s.name}"}} {ts.timestamp() if ts else 0}')
        return "\n".join(lines) + "\n"

    def make_server(self, host, port):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _redirect(self, location):
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                if path == "/hunt":
                    started = app.start_hunt()
                    self._redirect("/?hunt=" + ("started" if started else "bought" if app.store.bought() else "busy"))
                elif path in ("/track", "/buy"):
                    size = self.headers.get("Content-Length") or "0"
                    if not re.fullmatch(r"[0-9]{1,4}", size) or int(size) > 4096:  # rejects -1 (reads to EOF), "²"
                        self.send_error(400, "form too large or no length")
                        return
                    form = urllib.parse.parse_qs(self.rfile.read(int(size)).decode("utf-8", "replace"))
                    if path == "/buy":
                        self._redirect("/?hunt=" + ("bought" if app.mark_bought(form.get("machine", [""])[0]) else "gone"))
                        return
                    picked = clean_query(form.get("q", [""])[0], form.get("kind", [""])[0])
                    if picked is None:
                        self.send_error(400, "query and kind (disk, machine, cpu, ram or heatsink) required")
                        return
                    app.store.track(*picked)
                    self._redirect("/?hunt=tracked")
                else:
                    self.send_error(404)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
                notice, sort = query.get("hunt", [None])[0], query.get("sort", ["score"])[0]
                picked = clean_query(query.get("q", [""])[0], query.get("kind", [""])[0])
                if url.path == "/search" and picked is None:
                    self.send_error(400, "query and kind (disk, machine, cpu, ram or heatsink) required")
                    return
                routes = {"/": ("text/html; charset=utf-8", lambda: app.page(notice, sort)),
                          "/search": ("text/html; charset=utf-8", lambda: app.search_page(*picked)),
                          "/history": ("text/html; charset=utf-8", app.history_page),
                          "/metrics": ("text/plain; version=0.0.4", app.metrics),
                          "/healthz": ("text/plain", lambda: "ok\n")}
                if url.path not in routes:
                    self.send_error(404)
                    return
                ctype, render = routes[url.path]
                data = render().encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

        return ThreadingHTTPServer((host, port), Handler)
