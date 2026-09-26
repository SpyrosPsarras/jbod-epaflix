"""The Deal Finder app: runs Hunts and serves the page and /metrics."""
import datetime
import html
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import (CEILING_NOK, DISK_QUERIES, HUNT_INTERVAL_S, MACHINE_QUERIES, MAX_QUERY_CHARS,
                     PICKUP_MAX_MINUTES, PICKUP_NOK_PER_KM, SOURCE_PAUSE_S, TARGET_TIB)
from .builds import rank_builds
from .costs import OsrmRouter, cost_breakdown, machine_penalties
from .rules import Unreadable, read_disk, read_machine
from .store import Store

log = logging.getLogger("dealfinder")

CONDITION_LABEL = {"new": "New", "refurbished": "Refurbished", "used": "Used", "for_parts": "For parts"}
FACT_LABEL = {"capacity": "capacity", "form_factor": "3.5\" or 2.5\"", "disk_class": "Disk class",
              "condition": "condition", "quantity": "single-unit price", "shipping": "shipping to Norway",
              "generation": "generation", "bays_35": "3.5\" bay count",
              "price": "price (make an offer)", "location": "pickup place"}
COST_LABEL = {"shipping": "shipping", "shipping_estimate": "Fiks ferdig (est.)", "pickup_trip": "pickup trip",
              "vat": "VAT", "single_psu": "2nd PSU", "caddies": "caddies", "raid_only": "HBA", "no_rails": "rails",
              "psu_unknown": "2nd PSU (not stated)", "caddies_unknown": "caddies (not stated)",
              "controller_unknown": "HBA (controller not stated)", "rails_unknown": "rails (not stated)",
              "weak_seller": "weak seller +10%", "seller_unknown": "seller rating not stated +10%",
              "high_risk": "High-risk +20%"}


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
    parts += [f"{COST_LABEL[k]} {c[k]:,.0f}" for k in ("shipping", "shipping_estimate", "pickup_trip", "vat") if c.get(k)]
    parts += [f"{COST_LABEL.get(k, k)} {v:,.0f}" for k, v in (c.get("penalties") or {}).items()]
    return '<br><small class="costs">' + _e(" + ".join(parts)) + "</small>" if c else ""


def _row_state(row):
    """(class/data attributes, price-drop or gone note) for one ranked row."""
    if row["gone"]:
        return (' class="gone" data-gone="1"',
                f' <span class="note">gone, last seen {row["last_seen"]:%Y-%m-%d}, last seller price {float(row["price"]):,.0f} '
                f'{_e(row["currency"])}</span>')
    prev, now = row["prev_price"], row["price"]
    if prev is not None and now < prev:
        return (' data-drop="1"', f' <span class="drop" title="seller price dropped">&darr; seller price was {float(prev):,.0f} '
                                  f'{_e(row["currency"])}</span>')
    return "", ""


BUILD_SORT = {  # column -> (header, key); every column sortable, server-side, no JavaScript
    "score": ("Score (NOK/TiB)", lambda b: b.score),
    "landed": ("Landed NOK", lambda b: b.landed_nok),
    "usable": ("Usable TiB", lambda b: -b.usable_tib),
    "machine": ("Machine", lambda b: b.machine["title"].lower()),
    "disks": ("Disks", lambda b: (len(b.disks), b.capacity_tb)),
    "sources": ("Sources", lambda b: ",".join(sorted({b.machine["source"], *(d["source"] for d in b.disks)}))),
}


def _link(row):
    return f'<a href="{_e(_safe_url(row["url"]))}">{_e(row["title"])}</a>'


def _build_rows(builds, sort):
    rows = []
    for b in sorted(builds, key=BUILD_SORT[sort][1]):
        m = b.machine
        sources = ", ".join(sorted({m["source"], *(d["source"] for d in b.disks)}))
        items = [f"<li>Machine {_link(m)}: {b.parts['machine']:,.0f} NOK{_breakdown(m, b.parts['machine_penalties'])}</li>"]
        items += [f"<li>Disk {_link(d)}: {float(d['landed_nok']):,.0f} NOK{_breakdown(d)}</li>" for d in b.disks]
        rows.append(
            f'<tr data-build="{_e(m["source_id"])}" data-score="{b.score:.2f}" data-landed="{b.landed_nok:.2f}">'
            f"<td>{b.score:,.0f}</td><td>{b.landed_nok:,.0f}</td><td>{b.usable_tib:.1f}</td>"
            f"<td>{_link(m)}</td><td>{len(b.disks)} &times; {b.capacity_tb:g} TB</td><td>{_e(sources)}</td>"
            f"<td><details><summary>show</summary><ul>{''.join(items)}</ul>"
            f"<p>Total {b.landed_nok:,.0f} NOK = Machine {b.parts['machine']:,.0f} + Disks {b.parts['disks']:,.0f}"
            f" (a shared pickup place is driven once)</p></details></td></tr>")
    return "".join(rows)


def clean_query(query, kind):
    """(query, kind) as typed by the owner, normalised; None when unusable."""
    query = " ".join((query or "").replace("\x00", " ").lower().split())[:MAX_QUERY_CHARS]
    return (query, kind) if query and kind in ("disk", "machine") else None


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
        elif not (result.get("disk", 0) + result.get("machine", 0)):
            faults.append((name, "returned no Listings for any Tracked query"))
    return faults


class App:
    def __init__(self, db_uri, sources, fx, disk_queries=None, machine_queries=None, pause=SOURCE_PAUSE_S,
                 router=None):
        self.store = Store(db_uri)
        self.store.migrate()
        self.router = router or OsrmRouter(self.store)
        self.sources, self.fx, self.pause = sources, fx, pause
        disk_queries, machine_queries = disk_queries or DISK_QUERIES, machine_queries or MACHINE_QUERIES
        self.store.seed_tracked([(q, "disk", src) for src, qs in disk_queries.items() for q in qs]
                                + [(q, "machine", "") for q in machine_queries])
        self._hunt_lock = threading.Lock()  # one Hunt at a time, scheduled or by hand

    def start_hunt(self):
        """Start a Hunt in the background. False when one is already running."""
        if not self._hunt_lock.acquire(blocking=False):
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
        """Start a Hunt whenever one is due, forever (or until `stop` is set)."""
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                now = datetime.datetime.now(datetime.timezone.utc)
                if stop.wait(next_hunt_delay(self.store.last_started(), now, interval)):
                    break
                self.start_hunt()
            except Exception:  # e.g. Postgres failover: the scheduler must outlive it, or Hunts stop silently
                log.exception("scheduler pass failed; retrying")
            stop.wait(retry)  # let the Hunt record its start before the next delay is computed

    def hunt(self):
        """One pass over every Source. A failing Source is recorded and does not stop the others."""
        hunt_id = self.store.start_hunt()
        detail, ok, tracked = {}, True, self.store.tracked()
        for source in self.sources:
            counts = {"disk": 0, "machine": 0}
            try:
                work = [(r["query"], r["kind"]) for r in tracked if r["source"] in ("", source.name)
                        and (r["kind"] == "disk" or source.supports_machines)]
                for query, kind in work:
                    for listing in source.search(query, details=kind == "machine"):
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
        """The rules' verdict on one Listing: None when it is not a Disk/Machine at all, else a dict with
        facts, missing, qualifies, capacity_tb, landed_nok and costs. Hunts save it; Search only shows it."""
        if kind == "disk":
            facts = read_disk(listing.title, listing.condition)
        else:
            facts = read_machine(listing.title, listing.description, listing.condition)
        if facts is None:
            return None
        if listing.price <= 0:  # "make an offer": no price to rank
            facts = Unreadable((facts.missing if isinstance(facts, Unreadable) else []) + ["price"])
        if isinstance(facts, Unreadable):
            return {"facts": None, "missing": facts.missing, "qualifies": False, "capacity_tb": None,
                    "landed_nok": None, "costs": None}
        facts_json = asdict(facts)
        penalties = machine_penalties(facts_json) if kind == "machine" and facts.bays_35 is not None else None
        costs, problem = cost_breakdown(listing, self.fx, source.foreign, self.router, penalties)
        # unknown shipping or place is a missing fact; a pickup beyond the limit is a disqualifier (rejected)
        landed = None if problem else costs["total"]
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

    def search(self, query, kind):
        """Run one query on every Source now and score the results; nothing is saved.
        Returns (rows, faults): rows are dicts of the Listing fields plus the score, best first."""
        rows, faults = [], []
        for source in self.sources:
            if kind == "machine" and not source.supports_machines:
                continue
            try:
                for listing in source.search(query, details=kind == "machine"):
                    s = self._score(source, listing, kind)
                    if s is not None:
                        rows.append({**asdict(listing), **s})
            except Exception as exc:  # one failing Source must not hide the others' results
                log.exception("search on %s failed", source.name)
                faults.append((source.name, str(exc)[:200]))

        def rank(r):
            per = r["landed_nok"] / r["capacity_tb"] if r["landed_nok"] and r["capacity_tb"] else r["landed_nok"]
            return (not r["qualifies"], r["landed_nok"] is None, per or 0)
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
            landed, cap = r["landed_nok"], r["capacity_tb"]
            body.append(
                f'<tr data-result="{_e(r["source_id"])}" data-qualifies="{int(r["qualifies"])}"><td>{_e(r["source"])}</td>'
                f'<td><a href="{_e(_safe_url(r["url"]))}">{_e(r["title"])}</a>{_breakdown(r)}</td><td>{_e(verdict)}</td>'
                f'<td>{"" if landed is None else f"{landed:,.0f}"}</td>'
                f'<td>{f"{landed / cap:,.0f}" if landed and cap else ""}</td></tr>')
        body = "".join(body)
        fault = "".join(f'<p class="fault" data-fault="{_e(n)}">Source fault: <b>{_e(n)}</b> {_e(m)}</p>' for n, m in faults)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Search: {_e(query)}</title>
<style>body{{font-family:sans-serif;margin:2em}}td,th{{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}}
.fault{{background:#fde8e8;border-left:4px solid #c62828;padding:8px 12px}}</style></head><body>
<p><a href="/">&larr; Deal Finder</a></p><h1>Search: {_e(query)} ({_e(kind)}s)</h1>{fault}
<form method="post" action="/track"><input type="hidden" name="q" value="{_e(query)}">
<input type="hidden" name="kind" value="{_e(kind)}"><button>Track this query</button> (every Hunt will run it)</form>
<p>{len(rows)} results, {sum(r["qualifies"] for r in rows)} qualify. Scored with the same rules as a Hunt; nothing is saved.</p>
<table><tr><th>Source</th><th>Listing</th><th>Verdict</th><th>Landed NOK</th><th>NOK per TB</th></tr>{body}</table>
</body></html>"""

    def page(self, notice=None, sort="score"):
        sort = sort if sort in BUILD_SORT else "score"
        builds = rank_builds(*self.store.build_parts())
        build_head = "".join(f'<th><a href="/?sort={k}">{_e(label)}</a></th>' for k, (label, _) in BUILD_SORT.items())
        last = self.store.last_hunt()
        disks, machines, unreadable = self.store.best_disks(), self.store.best_machines(), self.store.unreadable()
        disk_rows = "".join(
            '<tr data-listing="{id}" data-nok-per-tb="{npt:.2f}"{attrs}><td><a href="{url}">{title}</a>{note}</td>'
            '<td>{cap:g} TB</td><td>{cond}</td><td>{where}</td><td>{landed:,.0f}</td><td>{npt:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), npt=float(r["nok_per_tb"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                cap=float(r["capacity_tb"]), cond=_e(CONDITION_LABEL.get(r["condition"], r["condition"] or "?")),
                where=_where(r), landed=float(r["landed_nok"]), attrs=state[0], note=state[1] + _breakdown(r))
            for r in disks for state in [_row_state(r)])
        machine_rows = "".join(
            '<tr data-machine="{id}" data-landed="{landed:.2f}"{attrs}><td><a href="{url}">{title}</a>{note}</td><td>{model}</td>'
            '<td>{gen}th</td><td>{bays}</td><td>{ram}</td><td>{psu}</td><td>{caddies}</td><td>{ctrl}</td>'
            '<td>{rails}</td><td>{where}</td><td>{landed:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), landed=float(r["landed_nok"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                model=_e(f["model"]), gen=_e(f["generation"]), bays=_e(f["bays_35"]),
                ram=_e(f"{f['ram_gb']} GB" if f["ram_gb"] else "?"), psu=_e(f["psu_count"] or "?"),
                caddies=_e("?" if f["caddies_35"] is None else f["caddies_35"]), ctrl=_e(f["controller"] or "?"),
                rails=_yes_no(f["rails"]), where=_where(r), attrs=state[0], note=state[1] + _breakdown(r))
            for r in machines for f in [r["facts"]] for state in [_row_state(r)])
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
                   "tracked": "Query tracked. Every Hunt runs it from now on."}
        tracked = self.store.tracked()
        tracked_list = {kind: " &middot; ".join(
            f'<span data-tracked="{_e(kind)}:{_e(r["query"])}">{_e(r["query"])}'
            + (f' <small>({_e(r["source"])} only)</small>' if r["source"] else "") + "</span>"
            for r in tracked if r["kind"] == kind) for kind in ("disk", "machine")}
        note = f'<p class="notice" data-notice="{_e(notice)}">{_e(notices[notice])}</p>' if notice in notices else ""
        running = '<p class="notice">A Hunt is running now.</p>' if self.hunt_running() else ""
        took = f" (took {(last['finished'] - last['started']).total_seconds():.0f} s)" if last else ""
        per_source = " &middot; ".join(f"{_e(s.name)}: last successful Hunt {_when(success.get(s.name))}"
                                       for s in self.sources)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder</title>
<style>body{{font-family:sans-serif;margin:2em}}table{{border-collapse:collapse;margin-bottom:2em}}
td,th{{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}}
.fault{{background:#fde8e8;border-left:4px solid #c62828;padding:8px 12px}}
.notice{{background:#e8f0fd;border-left:4px solid #1565c0;padding:8px 12px}}
tr.gone{{color:#999}} tr.gone a{{color:#999}} .note{{font-size:85%}} .drop{{color:#2e7d32;font-weight:bold}}</style></head><body>
<h1>Deal Finder</h1>{banner}{note}{running}
<p>Last Hunt: {_when(last["finished"] if last else None)}{took}. Hunts run every {HUNT_INTERVAL_S // 3600} hours.
<form method="post" action="/hunt" style="display:inline"><button>Hunt now</button></form></p>
<p>{per_source}</p>
<form method="get" action="/search"><input name="q" size="30" placeholder="e.g. exos x20 or r740xd" required
maxlength="{MAX_QUERY_CHARS}"> <select name="kind"><option value="disk">Disks</option><option value="machine">Machines</option>
</select> <button>Search every Source now</button></form>
<details><summary>{len(tracked)} Tracked queries run by every Hunt</summary>
<p><b>Disks:</b> {tracked_list["disk"]}</p><p><b>Machines:</b> {tracked_list["machine"]}</p></details>
<p>Landed cost = price + shipping or pickup trip from Sandefjord ({PICKUP_NOK_PER_KM} NOK/km, max {PICKUP_MAX_MINUTES} min one way) + import VAT + Penalties (unknown PSU, caddies, controller or rails are charged).</p>
<h2>Builds</h2><p>One Machine plus same-size Disks reaching {TARGET_TIB} TiB usable in RAIDZ2; the cheapest per
Machine, Builds over {CEILING_NOK:,} NOK hidden. Lower Score is better.</p>
<table><tr>{build_head}<th>Details</th></tr>{_build_rows(builds, sort)}</table>
<h2>Best Disks</h2><table><tr><th>Disk</th><th>Capacity</th><th>Condition</th><th>Where</th>
<th>Landed NOK</th><th>NOK per TB</th></tr>{disk_rows}</table>
<h2>Best Machines</h2><table><tr><th>Machine</th><th>Model</th><th>Gen</th><th>3.5" bays</th><th>RAM</th>
<th>PSUs</th><th>3.5" caddies</th><th>Controller</th><th>Rails</th><th>Where</th><th>Landed NOK</th></tr>{machine_rows}</table>
<h2>Could not read</h2><table><tr><th>Kind</th><th>Source</th><th>Listing</th><th>Missing</th></tr>{unreadable_rows}</table>
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
        lines.append("# TYPE dealfinder_hunt_running gauge")
        lines.append(f"dealfinder_hunt_running {int(self.hunt_running())}")
        faulty = {name for name, _ in source_faults(detail)}
        lines += ["# TYPE dealfinder_source_up gauge", "# TYPE dealfinder_source_listings gauge",
                  "# TYPE dealfinder_source_last_success_timestamp_seconds gauge"]
        for s in self.sources:
            result = detail.get(s.name, {})
            lines.append(f'dealfinder_source_up{{source="{s.name}"}} {int(s.name in detail and s.name not in faulty)}')
            lines.append(f'dealfinder_source_listings{{source="{s.name}"}} {result.get("disk", 0) + result.get("machine", 0)}')
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
                    self._redirect("/?hunt=" + ("started" if app.start_hunt() else "busy"))
                elif path == "/track":
                    size = self.headers.get("Content-Length") or "0"
                    if not re.fullmatch(r"[0-9]{1,4}", size) or int(size) > 4096:  # rejects -1 (reads to EOF), "²"
                        self.send_error(400, "form too large or no length")
                        return
                    form = urllib.parse.parse_qs(self.rfile.read(int(size)).decode("utf-8", "replace"))
                    picked = clean_query(form.get("q", [""])[0], form.get("kind", [""])[0])
                    if picked is None:
                        self.send_error(400, "query and kind (disk or machine) required")
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
                    self.send_error(400, "query and kind (disk or machine) required")
                    return
                routes = {"/": ("text/html; charset=utf-8", lambda: app.page(notice, sort)),
                          "/search": ("text/html; charset=utf-8", lambda: app.search_page(*picked)),
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
