"""Postgres store for Listings and Hunts."""
import json

import psycopg
from psycopg.rows import dict_row

from .config import GONE_DAYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source       text NOT NULL,
    source_id    text NOT NULL,
    kind         text NOT NULL,
    title        text NOT NULL,
    url          text NOT NULL,
    price        numeric NOT NULL,
    currency     text NOT NULL,
    shipping     numeric,
    condition    text,
    seller       text,
    facts        jsonb,
    missing      text[],
    qualifies    boolean NOT NULL DEFAULT false,
    capacity_tb  numeric,
    landed_nok   numeric,
    description  text,
    location     text,
    lat          double precision,
    lon          double precision,
    pickup_only  boolean NOT NULL DEFAULT false,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_id)
);
-- columns added after the first deploy (ticket #4)
ALTER TABLE listings ADD COLUMN IF NOT EXISTS description text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS location text;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS lat double precision;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS lon double precision;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS pickup_only boolean NOT NULL DEFAULT false;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS costs jsonb;
-- road km/minutes from home per rounded location (ticket #5)
CREATE TABLE IF NOT EXISTS routes (
    lat numeric NOT NULL, lon numeric NOT NULL, km numeric NOT NULL, minutes numeric NOT NULL,
    PRIMARY KEY (lat, lon)
);
-- one row per Listing per Hunt that saw it (ticket #9)
CREATE TABLE IF NOT EXISTS price_observations (
    source    text NOT NULL,
    source_id text NOT NULL,
    hunt_id   bigint NOT NULL,
    price     numeric NOT NULL,
    currency  text NOT NULL,
    seen_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_id, hunt_id)
);
-- queries every Hunt runs; source '' = every Source (ticket #8)
CREATE TABLE IF NOT EXISTS tracked_queries (
    query  text NOT NULL,
    kind   text NOT NULL,
    source text NOT NULL DEFAULT '',
    added  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (query, kind, source)
);
-- the Build the owner bought; any row stops all Hunts for good (ticket #10)
CREATE TABLE IF NOT EXISTS purchases (
    one    boolean PRIMARY KEY DEFAULT true CHECK (one),  -- at most one row: bought once, for good
    bought timestamptz NOT NULL DEFAULT now(),
    build  jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS hunts (
    id       bigserial PRIMARY KEY,
    started  timestamptz NOT NULL DEFAULT now(),
    finished timestamptz,
    ok       boolean,
    detail   jsonb
);
"""


# A Source's successful Hunt: it finished ok and found Listings. The one definition used by both the page
# tables (_FRESH) and the per-Source "last successful Hunt" line, so a failing or empty Source keeps showing
# the Listings from its last good Hunt, and the banner says so (ticket #7).
_SOURCE_OK = """
    h.finished IS NOT NULL AND (s.value->>'ok')::boolean
    AND coalesce((s.value->>'disk')::int, 0) + coalesce((s.value->>'machine')::int, 0) > 0
"""
_FRESH = f"""
WITH fresh AS (
    SELECT s.key AS source, max(h.started) AS since
    FROM hunts h, jsonb_each(h.detail) s
    WHERE {_SOURCE_OK}
    GROUP BY 1
)
"""


# Ranked rows: fresh Listings plus Gone Listings (missed by their Source's latest successful Hunt) for
# GONE_DAYS days; `prev_price` is the Source price at the Hunt before the latest one that saw the Listing.
# ponytail: the window scans every observation per page load (~100k rows/month, fine for one user for months);
# if the page slows, fetch the previous price per shown Listing with LATERAL ... OFFSET 1 LIMIT 1 or prune old rows
_RANKED = _FRESH + f""",
prev AS (
    SELECT source, source_id, price AS prev_price
    FROM (SELECT source, source_id, price,
                 row_number() OVER (PARTITION BY source, source_id ORDER BY hunt_id DESC) AS rn
          FROM price_observations) o
    WHERE rn = 2
)
"""
_RANKED_WHERE = f"""
    qualifies AND landed_nok IS NOT NULL
    AND (last_seen >= f.since OR last_seen > now() - interval '{int(GONE_DAYS)} days')
"""


class Store:
    def __init__(self, uri):
        self.uri = uri

    def _conn(self):
        # ponytail: one connection per call (~1k per Hunt, every 6h); batch per Source if Hunts get slow
        return psycopg.connect(self.uri, autocommit=True, row_factory=dict_row)

    def migrate(self):
        with self._conn() as c:
            c.execute(SCHEMA)

    def start_hunt(self):
        with self._conn() as c:
            return c.execute("INSERT INTO hunts DEFAULT VALUES RETURNING id").fetchone()["id"]

    def finish_hunt(self, hunt_id, ok, detail):
        with self._conn() as c:
            c.execute("UPDATE hunts SET finished = now(), ok = %s, detail = %s WHERE id = %s",
                      (ok, json.dumps(detail), hunt_id))

    def seed_tracked(self, rows):
        """Insert the starting Tracked queries (query, kind, source) when there are none yet (first start)."""
        with self._conn() as c, c.transaction():
            if c.execute("SELECT 1 FROM tracked_queries LIMIT 1").fetchone() is None:
                for row in rows:
                    c.execute("INSERT INTO tracked_queries (query, kind, source) VALUES (%s, %s, %s) "
                              "ON CONFLICT DO NOTHING", row)

    def track(self, query, kind):
        with self._conn() as c:
            c.execute("INSERT INTO tracked_queries (query, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (query, kind))

    def tracked(self):
        with self._conn() as c:
            return c.execute("SELECT query, kind, source FROM tracked_queries ORDER BY added, query").fetchall()

    def record_purchase(self, build):
        """True when recorded; False when a Build was already bought."""
        with self._conn() as c:
            return c.execute("INSERT INTO purchases (build) VALUES (%s) ON CONFLICT DO NOTHING",
                             (json.dumps(build),)).rowcount == 1

    def bought(self):
        """The recorded purchase {bought, build}, or None while hunting."""
        with self._conn() as c:
            return c.execute("SELECT bought, build FROM purchases").fetchone()

    def route_get(self, lat, lon):
        with self._conn() as c:
            row = c.execute("SELECT km, minutes FROM routes WHERE lat = %s AND lon = %s", (lat, lon)).fetchone()
            return (float(row["km"]), float(row["minutes"])) if row else None

    def route_put(self, lat, lon, km, minutes):
        with self._conn() as c:
            c.execute("INSERT INTO routes VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING", (lat, lon, km, minutes))

    def save_listing(self, listing, kind, facts, missing, qualifies, capacity_tb, landed, hunt_id, costs=None):
        with self._conn() as c:
            c.execute("""
                INSERT INTO price_observations (source, source_id, hunt_id, price, currency)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, source_id, hunt_id) DO UPDATE SET price = EXCLUDED.price
            """, (listing.source, listing.source_id, hunt_id, listing.price, listing.currency))
            c.execute("""
                INSERT INTO listings (source, source_id, kind, title, url, price, currency, shipping,
                                      condition, seller, facts, missing, qualifies, capacity_tb, landed_nok,
                                      description, location, lat, lon, pickup_only, costs)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, source_id) DO UPDATE SET
                    title = EXCLUDED.title, url = EXCLUDED.url, price = EXCLUDED.price,
                    currency = EXCLUDED.currency, shipping = EXCLUDED.shipping,
                    condition = EXCLUDED.condition, seller = EXCLUDED.seller, facts = EXCLUDED.facts,
                    missing = EXCLUDED.missing, qualifies = EXCLUDED.qualifies,
                    capacity_tb = EXCLUDED.capacity_tb, landed_nok = EXCLUDED.landed_nok,
                    description = EXCLUDED.description, location = EXCLUDED.location, lat = EXCLUDED.lat,
                    lon = EXCLUDED.lon, pickup_only = EXCLUDED.pickup_only, costs = EXCLUDED.costs,
                    last_seen = now()
            """, (listing.source, listing.source_id, kind, listing.title, listing.url, listing.price,
                  listing.currency, listing.shipping, listing.condition, listing.seller,
                  json.dumps(facts) if facts is not None else None, missing, qualifies, capacity_tb, landed,
                  listing.description, listing.location, listing.lat, listing.lon, listing.pickup_only,
                  json.dumps(costs) if costs is not None else None))

    def last_hunt(self):
        """The latest finished Hunt, successful or not."""
        with self._conn() as c:
            return c.execute("""SELECT id, started, finished, ok, detail FROM hunts
                                WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1""").fetchone()

    def last_started(self):
        with self._conn() as c:
            row = c.execute("SELECT max(started) AS started FROM hunts").fetchone()
            return row["started"]

    def source_last_success(self):
        """{source: finish time of its last successful Hunt that found Listings}."""
        with self._conn() as c:
            return {r["source"]: r["finished"] for r in c.execute(f"""
                SELECT s.key AS source, max(h.finished) AS finished
                FROM hunts h, jsonb_each(h.detail) s
                WHERE {_SOURCE_OK}
                GROUP BY 1""")}

    def best_disks(self, limit=100):
        with self._conn() as c:
            return c.execute(_RANKED + f"""
                SELECT l.source, l.source_id, title, url, capacity_tb, condition, landed_nok, location, pickup_only,
                       round(landed_nok / capacity_tb, 2) AS nok_per_tb, l.price, l.currency, p.prev_price, l.costs,
                       l.last_seen < f.since AS gone, l.last_seen
                FROM listings l JOIN fresh f ON f.source = l.source
                LEFT JOIN prev p ON p.source = l.source AND p.source_id = l.source_id
                WHERE kind = 'disk' AND {_RANKED_WHERE}
                ORDER BY gone, nok_per_tb, landed_nok LIMIT %s
            """, (limit,)).fetchall()

    def best_machines(self, limit=50):
        with self._conn() as c:
            return c.execute(_RANKED + f"""
                SELECT l.source, l.source_id, title, url, facts, landed_nok, location, pickup_only, l.costs,
                       l.price, l.currency, p.prev_price, l.last_seen < f.since AS gone, l.last_seen
                FROM listings l JOIN fresh f ON f.source = l.source
                LEFT JOIN prev p ON p.source = l.source AND p.source_id = l.source_id
                WHERE kind = 'machine' AND {_RANKED_WHERE}
                ORDER BY gone, landed_nok LIMIT %s
            """, (limit,)).fetchall()

    def build_parts(self):
        """Live (not Gone) qualified Machines and Disks, the inputs of the Build optimizer."""
        with self._conn() as c:
            rows = c.execute(_FRESH + """
                SELECT l.source, source_id, kind, title, url, facts, capacity_tb, landed_nok, location,
                       pickup_only, costs
                FROM listings l JOIN fresh f ON f.source = l.source
                WHERE qualifies AND landed_nok IS NOT NULL AND last_seen >= f.since
            """).fetchall()
        return [r for r in rows if r["kind"] == "machine"], [r for r in rows if r["kind"] == "disk"]

    def unreadable(self, limit=200):
        with self._conn() as c:
            return c.execute(_FRESH + """
                SELECT l.source, source_id, kind, title, url, missing
                FROM listings l JOIN fresh f ON f.source = l.source
                WHERE cardinality(missing) > 0 AND last_seen >= f.since
                ORDER BY kind DESC, l.source, title LIMIT %s
            """, (limit,)).fetchall()

    def counts(self):
        with self._conn() as c:
            return c.execute(_FRESH + """
                SELECT l.source, kind,
                       CASE WHEN qualifies THEN 'qualified'
                            WHEN cardinality(missing) > 0 THEN 'unreadable'
                            ELSE 'rejected' END AS state,
                       count(*) AS n
                FROM listings l JOIN fresh f ON f.source = l.source
                WHERE last_seen >= f.since GROUP BY 1, 2, 3
            """).fetchall()
