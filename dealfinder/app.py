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

from .config import (DISK_MIN_NOK_PER_TB, DISK_QUERIES, DISK_QUERY_ADDITIONS, HISTORY_WEEKS, HUNT_INTERVAL_S,
                     MAX_QUERY_CHARS, PICKUP_MAX_MINUTES, PICKUP_NOK_PER_KM, RAM_MIN_NOK_PER_GB,
                     RAM_QUERIES, SOURCE_PAUSE_S)
from .costs import OsrmRouter, cost_breakdown
from .rules import Unreadable, priced_per_unit, read_disk, read_ram, stock_in
from .store import Store, _history_key

log = logging.getLogger("dealfinder")

KINDS = ("disk", "ram")

CONDITION_LABEL = {"new": "New", "refurbished": "Refurbished", "used": "Used", "for_parts": "For parts"}
FACT_LABEL = {"capacity": "capacity", "form_factor": '3.5" or 2.5"', "condition": "condition",
              "quantity": "how many disks the price buys", "shipping": "shipping to Norway",
              "gb_per_stick": "GB per stick", "ddr": "DDR generation", "type": "RDIMM or LRDIMM",
              "price": "price (make an offer)", "location": "pickup place"}
COST_LABEL = {"shipping": "shipping", "shipping_estimate": "shipping (est.)", "finn_fee": "Trygg betaling (est.)",
              "pickup_trip": "pickup trip", "vat": "VAT", "weak_seller": "weak seller +10%",
              "seller_unknown": "seller rating not stated +10%"}


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
                 for n in range(1, 4))
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


def _breakdown(row):
    c = row.get("costs") or {}
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


def _link(row):
    return f'<a href="{_e(_safe_url(row["url"]))}">{_e(row["title"])}</a>'


def _bought_html(bought):
    if not bought:
        return ""
    return (f'<div class="bought"><h2>Bought {_when(bought["bought"])}</h2>'
            "<p>Hunting has stopped for good.</p></div>")


def _per_unit(row):
    """Landed NOK per TB for a Disk or per GB for RAM."""
    f = row["facts"] or {}
    unit = (row["capacity_tb"] * f.get("count", 1) if row["capacity_tb"]
            else (f.get("gb_per_stick") or 0) * f.get("sticks", 0))
    return row["landed_nok"] / unit if row["landed_nok"] and unit else None


def clean_query(query, kind):
    """(query, kind) as typed by the owner, normalised; None when unusable."""
    query = " ".join((query or "").replace("\x00", " ").lower().split())[:MAX_QUERY_CHARS]
    return (query, kind) if query and kind in KINDS else None


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
    def __init__(self, db_uri, sources, fx, disk_queries=None, ram_queries=None, pause=SOURCE_PAUSE_S, router=None):
        self.store = Store(db_uri)
        self.store.migrate()
        self.router = router or OsrmRouter(self.store)
        self.sources, self.fx, self.pause = sources, fx, pause
        for source in sources:  # eBay stock read by an earlier pod counts against the same daily quota
            if hasattr(source, "seed_items"):
                source.seed_items(self.store.item_read(source.name))
        additions = DISK_QUERY_ADDITIONS if disk_queries is None else {}
        disk_queries = DISK_QUERIES if disk_queries is None else disk_queries
        ram_queries = RAM_QUERIES if ram_queries is None else ram_queries
        self.store.seed_tracked([(q, "disk", src) for src, qs in disk_queries.items() for q in qs]
                                + [(q, "ram", src) for src, qs in ram_queries.items() for q in qs],
                                [(q, "disk", src) for src, qs in additions.items() for q in qs])
        self._hunt_lock = threading.Lock()  # one Hunt at a time, scheduled or by hand

    def start_hunt(self):
        """Start a Hunt in the background. False when one is already running or the purchase is done."""
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
            log.info("Bought: hunting has stopped")
            return
        hunt_id = self.store.start_hunt()
        detail, ok, tracked = {}, True, self.store.tracked()
        for source in self.sources:
            if self.store.bought():  # bought while this Hunt runs: stop before the next Source
                break
            counts = dict.fromkeys(KINDS, 0)
            try:
                if hasattr(source, "start_hunt"):
                    source.start_hunt()
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

    def _score(self, source, listing, kind):
        """The rules' verdict on one Listing: None when it is not of that kind at all, else a dict with
        facts, missing, qualifies, capacity_tb, landed_nok and costs. Hunts save it; Search only shows it."""
        if kind == "disk":
            facts = read_disk(listing.title, listing.condition,
                              aspects=listing.aspects, category_id=listing.category_id)
        elif kind == "ram":
            facts = read_ram(listing.title, listing.condition,
                             description=listing.description, aspects=listing.aspects, price=listing.price)
        else:
            return None
        if facts is None:
            return None
        if source.foreign and listing.shipping is None:
            return {"facts": None, "missing": [], "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        if listing.price <= 0 and not isinstance(facts, Unreadable) and not facts.qualifies:
            # "make an offer", but a known fact already rules it out (DDR3 RAM): rejected
            return {"facts": asdict(facts), "missing": [], "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        if listing.price <= 0:  # "make an offer": no price to rank
            facts = Unreadable((facts.missing if isinstance(facts, Unreadable) else []) + ["price"])
        if isinstance(facts, Unreadable):
            return {"facts": None, "missing": facts.missing, "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        facts_json = asdict(facts)
        unit = {"disk": "count", "ram": "sticks"}[kind]
        per_unit = priced_per_unit(listing.description, listing.price)
        if per_unit:
            facts_json[unit] = 1
        costs, problem = cost_breakdown(listing, self.fx, source.foreign, self.router, kind)
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

    def mark_bought(self):
        return self.store.record_purchase()

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
        unit = "GB" if kind == "ram" else "TB"
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
        series = self.store.history()
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder: price history</title>
{STYLE}</head><body><p><a href="/">&larr; Deal Finder</a></p><h1>Price history</h1>
<p>Qualifying Listings only, by Landed cost (price + shipping or pickup trip + VAT + seller-risk surcharge). Each cell is
lowest / median that week, with the number of Listings; each Listing counts once a week, at its lowest price.
History starts with this version (27 Sep 2026): earlier Hunts stored no Landed cost, so month-to-month comparison
needs a few weeks of data. The last {HISTORY_WEEKS} weeks are shown.</p>
{_tabs([
    ("Disks, NOK per TB", _history_table(series["disk"], lambda k: f"{k:g} TB", "Capacity")),
    ("RAM, NOK per GB", _history_table(series["ram"], str, "RAM"))])}
</body></html>"""

    def page(self, notice=None):
        last = self.store.last_hunt()
        disks, unreadable = self.store.best_disks(), self.store.unreadable()
        rams = self.store.best_ram()
        deals = self.store.deal_prices()

        def state(kind, r, per):
            unit = {"disk": "NOK per TB", "ram": "NOK per GB"}
            return _row_state(r, per, _deal(deals, kind, r), unit.get(kind, "NOK"))
        disk_rows = "".join(
            '<tr data-listing="{id}" data-nok-per-tb="{npt:.2f}"{attrs}><td><a href="{url}">{title}</a>{note}</td>'
            '<td>{lot}{cap:g} TB</td><td>{cond}</td><td>{where}</td><td>{landed:,.0f}</td><td>{npt:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), npt=float(r["nok_per_tb"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                cap=float(r["capacity_tb"]), lot=f'{r["count"]} &times; ' if (r["count"] or 1) > 1 else "",
                cond=_e(CONDITION_LABEL.get(r["condition"], r["condition"] or "?")),
                where=_where(r), landed=float(r["landed_nok"]), attrs=st[0], note=st[1] + _breakdown(r))
            for r in disks for st in [state("disk", r, float(r["nok_per_tb"]))])
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
        notices = {"started": "Hunt started. Refresh in a few minutes.",
                   "busy": "A Hunt is already running; this request was ignored.",
                   "tracked": "Query tracked. Every Hunt runs it from now on.",
                   "bought": "Bought; hunting has stopped for good.",
                   "gone": "The purchase was already recorded."}
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
            ("Best Disks", "<table><tr><th>Disk</th><th>Capacity</th><th>Condition</th><th>Where</th>"
                           f"<th>Landed NOK</th><th>NOK per TB</th></tr>{disk_rows}</table>"),
            ("Best RAM", "<table><tr><th>RAM</th><th>Type</th><th>Size</th><th>Speed</th><th>Where</th>"
                         f"<th>Landed NOK</th><th>NOK per GB</th></tr>{ram_rows}</table>"),
            ("Could not read", "<table><tr><th>Kind</th><th>Source</th><th>Listing</th><th>Missing</th></tr>"
                               f"{unreadable_rows}</table>")])
        buy = ("" if bought else '<form method="post" action="/buy" onsubmit="return confirm(\'Stop hunting for good?\')">'
               '<button>Mark as bought</button></form>')
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder</title>
{STYLE}</head><body>
<h1>Deal Finder</h1>{banner}{note}{running}
<p>Last Hunt: {_when(last["finished"] if last else None)}{took}. Hunts run every {HUNT_INTERVAL_S // 3600} hours.
{"" if bought else '<form method="post" action="/hunt" style="display:inline"><button>Hunt now</button></form>'}</p>
{_bought_html(bought)}
{buy}
<p>{per_source} &middot; <a href="/history">Price history</a></p>
<form method="get" action="/search"><input name="q" size="30" placeholder="e.g. purple 12tb" required
maxlength="{MAX_QUERY_CHARS}"> <select name="kind"><option value="disk">Disks</option><option value="ram">RAM</option></select> <button>Search every Source now</button></form>
<details><summary>{len(tracked)} Tracked queries run by every Hunt</summary>
<p><b>Disks:</b> {tracked_list["disk"]}</p><p><b>RAM:</b> {tracked_list["ram"]}</p></details>
<p>Landed cost = price + shipping or pickup trip from Sandefjord ({PICKUP_NOK_PER_KM} NOK/km, max {PICKUP_MAX_MINUTES} min one way) + finn.no Trygg betaling when shipped + import VAT + eBay seller-risk surcharge.</p>
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
        lines.append("# TYPE dealfinder_bought gauge")  # 1 once bought and hunting has stopped
        lines.append(f"dealfinder_bought {int(self.store.bought() is not None)}")
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
                        self._redirect("/?hunt=" + ("bought" if app.mark_bought() else "gone"))
                        return
                    picked = clean_query(form.get("q", [""])[0], form.get("kind", [""])[0])
                    if picked is None:
                        self.send_error(400, "query and kind (disk or ram) required")
                        return
                    app.store.track(*picked)
                    self._redirect("/?hunt=tracked")
                else:
                    self.send_error(404)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
                notice = query.get("hunt", [None])[0]
                picked = clean_query(query.get("q", [""])[0], query.get("kind", [""])[0])
                if url.path == "/search" and picked is None:
                    self.send_error(400, "query and kind (disk or ram) required")
                    return
                routes = {"/": ("text/html; charset=utf-8", lambda: app.page(notice)),
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
