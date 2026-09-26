"""The Deal Finder app: runs Hunts and serves the page and /metrics."""
import html
import logging
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import DISK_QUERIES
from .costs import landed_nok
from .rules import Unreadable, read_disk
from .store import Store

log = logging.getLogger("dealfinder")

CONDITION_LABEL = {"new": "New", "refurbished": "Refurbished", "used": "Used", "for_parts": "For parts"}


def _safe_url(url):
    """Only https links reach the page; anything else (javascript:, data:) becomes inert."""
    return url if url.startswith("https://") else "#"


class App:
    def __init__(self, db_uri, sources, fx, queries=None, pause=0.5):
        self.store = Store(db_uri)
        self.store.migrate()
        self.sources, self.fx, self.pause = sources, fx, pause
        self.queries = queries or DISK_QUERIES

    def hunt(self):
        """One pass over every Source. A failing Source is recorded and does not stop the others."""
        hunt_id = self.store.start_hunt()
        detail, ok = {}, True
        for source in self.sources:
            seen = 0
            try:
                for query in self.queries:
                    for listing in source.search(query):
                        seen += self._record(source, listing)
                    time.sleep(self.pause)
                detail[source.name] = {"ok": True, "listings": seen}
            except Exception as exc:  # a Source fault must not end the Hunt
                log.exception("source %s failed", source.name)
                detail[source.name] = {"ok": False, "error": str(exc)[:300], "listings": seen}
                ok = False
        self.store.finish_hunt(hunt_id, ok, detail)
        log.info("hunt %s done ok=%s %s", hunt_id, ok, detail)

    def _record(self, source, listing):
        facts = read_disk(listing.title, listing.condition)
        if facts is None:
            return 0
        landed = landed_nok(listing, self.fx, source.foreign)
        if isinstance(facts, Unreadable):
            missing, facts_json, qualifies, capacity = facts.missing, None, False, None
        else:
            missing = [] if landed is not None else ["shipping"]
            facts_json, capacity = asdict(facts), facts.capacity_tb
            qualifies = facts.qualifies and landed is not None
        self.store.save_listing(listing, "disk", facts_json, missing, qualifies, capacity, landed)
        return 1

    def page(self):
        last = self.store.last_ok_hunt()
        rows = self.store.best_disks(last["started"]) if last else []
        body = "".join(
            '<tr data-listing="{id}" data-nok-per-tb="{npt:.2f}"><td><a href="{url}">{title}</a></td>'
            '<td>{cap:g} TB</td><td>{cond}</td><td>{src}</td><td>{landed:,.0f}</td><td>{npt:,.0f}</td></tr>'.format(
                id=html.escape(r["source_id"]), npt=float(r["nok_per_tb"]), url=html.escape(_safe_url(r["url"])),
                title=html.escape(r["title"]), cap=float(r["capacity_tb"]),
                cond=html.escape(CONDITION_LABEL.get(r["condition"], r["condition"] or "?")), src=html.escape(r["source"]),
                landed=float(r["landed_nok"]))
            for r in rows)
        when = last["finished"].strftime("%Y-%m-%d %H:%M UTC") if last else "never"
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Deal Finder</title>
<style>body{{font-family:sans-serif;margin:2em}}table{{border-collapse:collapse}}
td,th{{padding:4px 10px;border-bottom:1px solid #ddd;text-align:left}}</style></head><body>
<h1>Deal Finder</h1><p>Last Hunt: {when}</p>
<h2>Best Disks</h2><table><tr><th>Disk</th><th>Capacity</th><th>Condition</th><th>Source</th>
<th>Landed NOK</th><th>NOK per TB</th></tr>{body}</table></body></html>"""

    def metrics(self):
        last = self.store.last_ok_hunt()
        lines = ["# TYPE dealfinder_listings gauge"]
        for r in (self.store.counts(last["started"]) if last else []):
            lines.append(f'dealfinder_listings{{source="{r["source"]}",state="{r["state"]}"}} {r["n"]}')
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
