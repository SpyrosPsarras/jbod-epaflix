"""Postgres store for Listings and Hunts."""
import json

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DISK_QUERY_UPDATE, GONE_DAYS, HISTORY_WEEKS

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
ALTER TABLE listings ADD COLUMN IF NOT EXISTS item_cache jsonb;
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
-- any purchase row stops all Hunts for good (ticket #10)
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
-- price history (#13): each observation's Landed cost (NULL unless the Listing qualified);
-- recorded from this version on, earlier observations have the seller price only
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS landed_nok numeric;
-- what the Listing was when observed, so a later retitle or disqualification does not move old prices
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS capacity_tb numeric;
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS model text;
-- RAM price history: each observation's kind, its group key (in `model`) and the units its
-- Landed cost is divided by (GB of RAM)
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE price_observations ADD COLUMN IF NOT EXISTS units numeric;
-- a removed Source
DELETE FROM tracked_queries WHERE source = 'aliexpress';
DELETE FROM listings WHERE source = 'aliexpress';
CREATE TABLE IF NOT EXISTS query_updates (name text PRIMARY KEY);
"""


# A Source's successful Hunt: it finished ok and found Listings. The one definition used by both the page
# tables (_FRESH) and the per-Source "last successful Hunt" line, so a failing or empty Source keeps showing
# the Listings from its last good Hunt, and the banner says so (ticket #7).
_SOURCE_OK = """
    h.finished IS NOT NULL AND (s.value->>'ok')::boolean
    AND coalesce((s.value->>'disk')::int, 0) + coalesce((s.value->>'ram')::int, 0) > 0
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
# GONE_DAYS days; `prev_price` is the Source price at the Hunt before the latest one that saw the Listing, one
# primary-key lookup per candidate row (a window over every observation took ~280 ms per table at 35k rows)
_PREV = """
LEFT JOIN LATERAL (SELECT o.price AS prev_price FROM price_observations o
                   WHERE o.source = l.source AND o.source_id = l.source_id
                   ORDER BY o.hunt_id DESC OFFSET 1 LIMIT 1) p ON true
"""
_RANKED_WHERE = f"""
    qualifies AND landed_nok IS NOT NULL
    AND (last_seen >= f.since OR last_seen > now() - interval '{int(GONE_DAYS)} days')
"""


def _history_key(kind, facts):
    """(group key, units) of a qualifying Listing on the price history page. A Disk groups by capacity instead; its
    units are the disks of a lot."""
    if kind == "ram":
        return f"DDR{facts['ddr']} {facts['type']} {facts['gb_per_stick']} GB", facts["gb_per_stick"] * facts["sticks"]
    return None, facts.get("count", 1)


class Store:
    def __init__(self, uri):
        self.uri = uri
        # a TLS connect costs ~100 ms and a query ~3 ms: a Hunt saves ~4k Listings, a page makes ~10 calls. The
        # Hunt, the scheduler and every request thread share the pool. check= replaces connections broken by a
        # Postgres failover. ponytail: 4 connections; raise max_size if page loads wait on the pool during a Hunt
        self._pool = ConnectionPool(uri, min_size=1, max_size=4, open=True, check=ConnectionPool.check_connection,
                                    kwargs={"autocommit": True, "row_factory": dict_row})

    def _conn(self):
        return self._pool.connection()

    def migrate(self):
        with self._conn() as c:
            c.execute(SCHEMA)
            old = c.execute("""
                SELECT o.source, o.source_id, o.hunt_id, l.kind, l.facts
                FROM price_observations o JOIN listings l USING (source, source_id)
                WHERE o.kind IS NULL AND o.landed_nok IS NOT NULL AND l.qualifies
                  AND l.kind = 'ram'""").fetchall()
            c.cursor().executemany(
                "UPDATE price_observations SET kind = %s, model = %s, units = %s "
                "WHERE source = %s AND source_id = %s AND hunt_id = %s",
                [(r["kind"], *_history_key(r["kind"], r["facts"]), r["source"], r["source_id"], r["hunt_id"])
                 for r in old])
            c.execute("""UPDATE price_observations o SET kind = l.kind FROM listings l
                         WHERE o.kind IS NULL AND l.kind IN ('disk', 'ram')
                           AND l.source = o.source AND l.source_id = o.source_id""")

    def start_hunt(self):
        with self._conn() as c:
            return c.execute("INSERT INTO hunts DEFAULT VALUES RETURNING id").fetchone()["id"]

    def finish_hunt(self, hunt_id, ok, detail):
        with self._conn() as c:
            c.execute("UPDATE hunts SET finished = now(), ok = %s, detail = %s WHERE id = %s",
                      (ok, json.dumps(detail), hunt_id))

    def seed_tracked(self, rows, additions=()):
        """Seed empty (kind, Source) groups, plus the 12TB/all-class additions once per database."""
        with self._conn() as c, c.transaction():
            have = {(r["kind"], r["source"]) for r in c.execute("SELECT DISTINCT kind, source FROM tracked_queries")}
            seed = [row for row in rows if (row[1], row[2]) not in have]
            if additions and c.execute("INSERT INTO query_updates VALUES (%s) "
                                       "ON CONFLICT DO NOTHING RETURNING name", (DISK_QUERY_UPDATE,)).fetchone():
                seed.extend(additions)
            c.cursor().executemany("INSERT INTO tracked_queries (query, kind, source) VALUES (%s, %s, %s) "
                                   "ON CONFLICT DO NOTHING", seed)

    def track(self, query, kind):
        with self._conn() as c:
            c.execute("INSERT INTO tracked_queries (query, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (query, kind))

    def tracked(self):
        with self._conn() as c:
            return c.execute("SELECT query, kind, source FROM tracked_queries "
                             "WHERE kind IN ('disk', 'ram') ORDER BY added, query").fetchall()

    def history(self):
        """Weekly lowest and median Landed NOK per TB for Disks and per GB for RAM."""
        since = f"now() - interval '{int(HISTORY_WEEKS)} weeks'"
        def weekly(kind, key, value):  # each Listing once per week, at its lowest price, grouped by `key`
            return c.execute(f"""
                SELECT week, key, source, count(*) AS n, min(v)::float AS low,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS median
                FROM (SELECT date_trunc('week', seen_at)::date AS week, {key} AS key, source, source_id,
                             min({value}) AS v
                      FROM price_observations
                      WHERE kind = %s AND {key} IS NOT NULL AND {value} IS NOT NULL AND seen_at > {since}
                      GROUP BY 1, 2, 3, 4) w
                GROUP BY 1, 2, 3
            """, (kind,)).fetchall()
        with self._conn() as c:
            return {"disk": weekly("disk", "capacity_tb::float", "landed_nok / (capacity_tb * coalesce(units, 1))"),
                    "ram": weekly("ram", "model", "landed_nok / units")}

    def deal_prices(self, min_listings=5):
        """{(kind, key): the lowest per-unit Landed cost of its earlier weeks}: the price a live Listing must beat to
        be a deal. Per unit as on the history page, all Sources pooled, the HISTORY_WEEKS weeks before the current
        week; a group seen in fewer than `min_listings` Listings in those weeks has no deal price. The key is the
        capacity in TB for a Disk, else the history key (_history_key)."""
        with self._conn() as c:
            rows = c.execute(f"""
                SELECT kind, model, capacity_tb::float AS cap,
                       min(landed_nok / CASE kind WHEN 'disk' THEN capacity_tb * coalesce(units, 1)
                                                  ELSE units END)::float AS low
                FROM price_observations
                WHERE kind IN ('disk', 'ram') AND landed_nok IS NOT NULL AND seen_at > now() - interval '{int(HISTORY_WEEKS)} weeks'
                  AND seen_at < date_trunc('week', now())
                  AND CASE kind WHEN 'disk' THEN capacity_tb IS NOT NULL ELSE model IS NOT NULL AND
                                                 units IS NOT NULL END
                GROUP BY 1, 2, 3 HAVING count(DISTINCT source || '|' || source_id) >= %s
            """, (min_listings,)).fetchall()
        return {(r["kind"], r["cap"] if r["kind"] == "disk" else r["model"]): r["low"] for r in rows}

    def record_purchase(self):
        with self._conn() as c:
            return c.execute("INSERT INTO purchases (build) VALUES ('{}') ON CONFLICT DO NOTHING").rowcount == 1

    def bought(self):
        with self._conn() as c:
            return c.execute("SELECT bought FROM purchases").fetchone()

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
                INSERT INTO price_observations (source, source_id, hunt_id, price, currency, kind, landed_nok,
                                                capacity_tb, model, units)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, source_id, hunt_id) DO UPDATE SET price = EXCLUDED.price, kind = EXCLUDED.kind,
                    landed_nok = EXCLUDED.landed_nok, capacity_tb = EXCLUDED.capacity_tb, model = EXCLUDED.model,
                    units = EXCLUDED.units
            """, (listing.source, listing.source_id, hunt_id, listing.price, listing.currency, kind,
                  *((landed, capacity_tb, *_history_key(kind, facts)) if qualifies else (None, None, None, None))))
            c.execute("""
                INSERT INTO listings (source, source_id, kind, title, url, price, currency, shipping,
                                      condition, seller, facts, missing, qualifies, capacity_tb, landed_nok,
                                      description, location, lat, lon, pickup_only, costs, item_cache)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, source_id) DO UPDATE SET
                    title = EXCLUDED.title, url = EXCLUDED.url, price = EXCLUDED.price,
                    currency = EXCLUDED.currency, shipping = EXCLUDED.shipping,
                    condition = EXCLUDED.condition, seller = EXCLUDED.seller, facts = EXCLUDED.facts,
                    missing = EXCLUDED.missing, qualifies = EXCLUDED.qualifies,
                    capacity_tb = EXCLUDED.capacity_tb, landed_nok = EXCLUDED.landed_nok,
                    description = EXCLUDED.description, location = EXCLUDED.location, lat = EXCLUDED.lat,
                    lon = EXCLUDED.lon, pickup_only = EXCLUDED.pickup_only, costs = EXCLUDED.costs,
                    item_cache = coalesce(EXCLUDED.item_cache, listings.item_cache), last_seen = now()
            """, (listing.source, listing.source_id, kind, listing.title, listing.url, listing.price,
                  listing.currency, listing.shipping, listing.condition, listing.seller,
                  json.dumps(facts) if facts is not None else None, missing, qualifies, capacity_tb, landed,
                  listing.description, listing.location, listing.lat, listing.lon, listing.pickup_only,
                  json.dumps(costs) if costs is not None else None,
                  json.dumps({"read": listing.item_read, "details": listing.item_details})
                  if listing.item_read is not None else None))

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
            return c.execute(_FRESH + f"""
                SELECT l.source, l.source_id, title, url, capacity_tb, condition, landed_nok, location, pickup_only,
                       round(landed_nok / (capacity_tb * coalesce((facts->>'count')::numeric, 1)), 2) AS nok_per_tb,
                       (facts->>'count')::int AS count, l.price, l.currency, p.prev_price, l.costs,
                       l.last_seen < f.since AS gone, l.last_seen
                FROM listings l JOIN fresh f ON f.source = l.source {_PREV}
                WHERE kind = 'disk' AND {_RANKED_WHERE}
                ORDER BY gone, nok_per_tb, landed_nok LIMIT %s
            """, (limit,)).fetchall()

    def best_ram(self, limit=50):
        """RAM ranked by Landed NOK per GB."""
        with self._conn() as c:
            return c.execute(_FRESH + f"""
                SELECT l.source, l.source_id, title, url, facts, landed_nok, location, pickup_only, l.costs,
                       l.price, l.currency, p.prev_price, l.last_seen < f.since AS gone, l.last_seen
                FROM listings l JOIN fresh f ON f.source = l.source {_PREV}
                WHERE kind = 'ram' AND {_RANKED_WHERE}
                ORDER BY gone, landed_nok / ((facts->>'gb_per_stick')::numeric * (facts->>'sticks')::numeric),
                         landed_nok LIMIT %s
            """, (limit,)).fetchall()

    def item_read(self, source):
        with self._conn() as c:
            return [(r["source_id"], r["item"]["read"], r["item"]["details"])
                    for r in c.execute("""
                        SELECT source_id, coalesce(item_cache, jsonb_build_object(
                            'read', facts->'stock_read',
                            'details', jsonb_build_object('_stock', jsonb_build_array(
                                facts->'stock', facts->'extra_shipping')))) AS item
                        FROM listings WHERE source = %s
                          AND (item_cache IS NOT NULL OR facts->>'stock_read' IS NOT NULL)
                    """, (source,))]

    def unreadable(self, limit=200):
        with self._conn() as c:
            return c.execute(_FRESH + """
                SELECT l.source, source_id, kind, title, url, missing
                FROM listings l JOIN fresh f ON f.source = l.source
                WHERE kind IN ('disk', 'ram') AND cardinality(missing) > 0 AND last_seen >= f.since
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
                WHERE kind IN ('disk', 'ram') AND last_seen >= f.since GROUP BY 1, 2, 3
            """).fetchall()
