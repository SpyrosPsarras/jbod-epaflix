"""The Deal Finder app: runs Hunts and serves the page and /metrics."""
import html
import logging
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DISK_QUERIES, MACHINE_QUERIES, SOURCE_PAUSE_S
from .costs import landed_nok
from .rules import Unreadable, read_disk, read_machine
from .store import Store

log = logging.getLogger("dealfinder")

CONDITION_LABEL = {"new": "New", "refurbished": "Refurbished", "used": "Used", "for_parts": "For parts"}
FACT_LABEL = {"capacity": "capacity", "form_factor": "3.5\" or 2.5\"", "disk_class": "Disk class",
              "condition": "condition", "quantity": "single-unit price", "shipping": "shipping to Norway",
              "generation": "generation", "bays_35": "3.5\" bay count",
              "price": "price (make an offer)"}


def _safe_url(url):
    """Only https links reach the page; anything else (javascript:, data:) becomes inert."""
    return url if url.startswith("https://") else "#"


def _e(value):
    return html.escape("" if value is None else str(value))


def _where(row):
    return _e(row["source"]) + (" &middot; pickup " + _e(row["location"]) if row["pickup_only"] else "")


def _yes_no(value):
    return {True: "yes", False: "no"}.get(value, "?")


class App:
    def __init__(self, db_uri, sources, fx, disk_queries=None, machine_queries=None, pause=SOURCE_PAUSE_S):
        self.store = Store(db_uri)
        self.store.migrate()
        self.sources, self.fx, self.pause = sources, fx, pause
        self.disk_queries = disk_queries or DISK_QUERIES
        self.machine_queries = machine_queries or MACHINE_QUERIES

    def hunt(self):
        """One pass over every Source. A failing Source is recorded and does not stop the others."""
        hunt_id = self.store.start_hunt()
        detail, ok = {}, True
        for source in self.sources:
            counts = {"disk": 0, "machine": 0}
            try:
                work = [(q, "disk") for q in self.disk_queries.get(source.name, [])]
                if source.supports_machines:
                    work += [(q, "machine") for q in self.machine_queries]
                for query, kind in work:
                    for listing in source.search(query, details=kind == "machine"):
                        counts[kind] += self._record(source, listing, kind)
                    time.sleep(self.pause)
                detail[source.name] = {"ok": True, **counts}
            except Exception as exc:  # a Source fault must not end the Hunt
                log.exception("source %s failed", source.name)
                detail[source.name] = {"ok": False, "error": str(exc)[:300], **counts}
                ok = False
        self.store.finish_hunt(hunt_id, ok, detail)
        log.info("hunt %s done ok=%s %s", hunt_id, ok, detail)

    def _record(self, source, listing, kind):
        if kind == "disk":
            facts = read_disk(listing.title, listing.condition)
        else:
            facts = read_machine(listing.title, listing.description, listing.condition)
        if facts is None:
            return 0
        if listing.price <= 0:  # "make an offer": no price to rank
            facts = Unreadable((facts.missing if isinstance(facts, Unreadable) else []) + ["price"])
        landed = landed_nok(listing, self.fx, source.foreign)
        if isinstance(facts, Unreadable):
            missing, facts_json, qualifies, capacity = facts.missing, None, False, None
        else:
            missing = [] if landed is not None else ["shipping"]
            facts_json = asdict(facts)
            capacity = facts.capacity_tb if kind == "disk" else None
            qualifies = facts.qualifies and landed is not None
        self.store.save_listing(listing, kind, facts_json, missing, qualifies, capacity, landed)
        return 1

    def page(self):
        last = self.store.last_ok_hunt()
        since = last["started"] if last else None
        disks = self.store.best_disks(since) if last else []
        machines = self.store.best_machines(since) if last else []
        unreadable = self.store.unreadable(since) if last else []
        disk_rows = "".join(
            '<tr data-listing="{id}" data-nok-per-tb="{npt:.2f}"><td><a href="{url}">{title}</a></td>'
            '<td>{cap:g} TB</td><td>{cond}</td><td>{where}</td><td>{landed:,.0f}</td><td>{npt:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), npt=float(r["nok_per_tb"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                cap=float(r["capacity_tb"]), cond=_e(CONDITION_LABEL.get(r["condition"], r["condition"] or "?")),
                where=_where(r), landed=float(r["landed_nok"]))
            for r in disks)
        machine_rows = "".join(
            '<tr data-machine="{id}" data-landed="{landed:.2f}"><td><a href="{url}">{title}</a></td><td>{model}</td>'
            '<td>{gen}th</td><td>{bays}</td><td>{ram}</td><td>{psu}</td><td>{caddies}</td><td>{ctrl}</td>'
            '<td>{rails}</td><td>{where}</td><td>{landed:,.0f}</td></tr>'.format(
                id=_e(r["source_id"]), landed=float(r["landed_nok"]), url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                model=_e(f["model"]), gen=_e(f["generation"]), bays=_e(f["bays_35"]),
                ram=_e(f"{f['ram_gb']} GB" if f["ram_gb"] else "?"), psu=_e(f["psu_count"] or "?"),
                caddies=_e("?" if f["caddies_35"] is None else f["caddies_35"]), ctrl=_e(f["controller"] or "?"),
                rails=_yes_no(f["rails"]), where=_where(r))
            for r in machines for f in [r["facts"]])
        unreadable_rows = "".join(
            '<tr data-unreadable="{id}" data-missing="{missing}"><td>{kind}</td><td>{src}</td>'
            '<td><a href="{url}">{title}</a></td><td>{labels}</td></tr>'.format(
                id=_e(r["source_id"]), missing=_e(",".join(r["missing"])), kind=_e(r["kind"]), src=_e(r["source"]),
                url=_e(_safe_url(r["url"])), title=_e(r["title"]),
                labels=_e(", ".join(FACT_LABEL.get(m, m) for m in r["missing"])))
            for r in unreadable)
        when = last["finished"].strftime("%Y-%m-%d %H:%M UTC") if last else "never"
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder</title>
<style>body{{font-family:sans-serif;margin:2em}}table{{border-collapse:collapse;margin-bottom:2em}}
td,th{{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}}</style></head><body>
<h1>Deal Finder</h1><p>Last Hunt: {when}. Pickup trips and Penalties are not priced in yet.</p>
<h2>Best Disks</h2><table><tr><th>Disk</th><th>Capacity</th><th>Condition</th><th>Where</th>
<th>Landed NOK</th><th>NOK per TB</th></tr>{disk_rows}</table>
<h2>Best Machines</h2><table><tr><th>Machine</th><th>Model</th><th>Gen</th><th>3.5" bays</th><th>RAM</th>
<th>PSUs</th><th>3.5" caddies</th><th>Controller</th><th>Rails</th><th>Where</th><th>Landed NOK</th></tr>{machine_rows}</table>
<h2>Could not read</h2><table><tr><th>Kind</th><th>Source</th><th>Listing</th><th>Missing</th></tr>{unreadable_rows}</table>
</body></html>"""

    def metrics(self):
        last = self.store.last_ok_hunt()
        lines = ["# TYPE dealfinder_listings gauge"]
        for r in (self.store.counts(last["started"]) if last else []):
            lines.append(f'dealfinder_listings{{source="{r["source"]}",kind="{r["kind"]}",state="{r["state"]}"}} {r["n"]}')
        lines.append("# TYPE dealfinder_hunt_last_success_timestamp_seconds gauge")
        lines.append(f"dealfinder_hunt_last_success_timestamp_seconds {last['finished'].timestamp() if last else 0}")
        return "\n".join(lines) + "\n"

    def make_server(self, host, port):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                routes = {"/": ("text/html; charset=utf-8", app.page),
                          "/metrics": ("text/plain; version=0.0.4", app.metrics),
                          "/healthz": ("text/plain", lambda: "ok\n")}
                if self.path not in routes:
                    self.send_error(404)
                    return
                ctype, render = routes[self.path]
                data = render().encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

        return ThreadingHTTPServer((host, port), Handler)

    def hunt_in_background(self):
        threading.Thread(target=self.hunt, name="hunt", daemon=True).start()
