"""Postgres store for Listings and Hunts."""
import json

import psycopg
from psycopg.rows import dict_row

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
CREATE TABLE IF NOT EXISTS hunts (
    id       bigserial PRIMARY KEY,
    started  timestamptz NOT NULL DEFAULT now(),
    finished timestamptz,
    ok       boolean,
    detail   jsonb
);
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

    def save_listing(self, listing, kind, facts, missing, qualifies, capacity_tb, landed):
        with self._conn() as c:
            c.execute("""
                INSERT INTO listings (source, source_id, kind, title, url, price, currency, shipping,
                                      condition, seller, facts, missing, qualifies, capacity_tb, landed_nok,
                                      description, location, lat, lon, pickup_only)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source, source_id) DO UPDATE SET
                    title = EXCLUDED.title, url = EXCLUDED.url, price = EXCLUDED.price,
                    currency = EXCLUDED.currency, shipping = EXCLUDED.shipping,
                    condition = EXCLUDED.condition, seller = EXCLUDED.seller, facts = EXCLUDED.facts,
                    missing = EXCLUDED.missing, qualifies = EXCLUDED.qualifies,
                    capacity_tb = EXCLUDED.capacity_tb, landed_nok = EXCLUDED.landed_nok,
                    description = EXCLUDED.description, location = EXCLUDED.location, lat = EXCLUDED.lat,
                    lon = EXCLUDED.lon, pickup_only = EXCLUDED.pickup_only, last_seen = now()
            """, (listing.source, listing.source_id, kind, listing.title, listing.url, listing.price,
                  listing.currency, listing.shipping, listing.condition, listing.seller,
                  json.dumps(facts) if facts is not None else None, missing, qualifies, capacity_tb, landed,
                  listing.description, listing.location, listing.lat, listing.lon, listing.pickup_only))

    def last_ok_hunt(self):
        with self._conn() as c:
            return c.execute("SELECT id, started, finished FROM hunts WHERE ok ORDER BY id DESC LIMIT 1").fetchone()

    def best_disks(self, since, limit=100):
        with self._conn() as c:
            return c.execute("""
                SELECT source, source_id, title, url, capacity_tb, condition, landed_nok, location, pickup_only,
                       round(landed_nok / capacity_tb, 2) AS nok_per_tb
                FROM listings
                WHERE kind = 'disk' AND qualifies AND landed_nok IS NOT NULL AND last_seen >= %s
                ORDER BY nok_per_tb, landed_nok LIMIT %s
            """, (since, limit)).fetchall()

    def best_machines(self, since, limit=50):
        with self._conn() as c:
            return c.execute("""
                SELECT source, source_id, title, url, facts, landed_nok, location, pickup_only
                FROM listings
                WHERE kind = 'machine' AND qualifies AND landed_nok IS NOT NULL AND last_seen >= %s
                ORDER BY landed_nok LIMIT %s
            """, (since, limit)).fetchall()

    def unreadable(self, since, limit=200):
        with self._conn() as c:
            return c.execute("""
                SELECT source, source_id, kind, title, url, missing
                FROM listings WHERE cardinality(missing) > 0 AND last_seen >= %s
                ORDER BY kind DESC, source, title LIMIT %s
            """, (since, limit)).fetchall()

    def counts(self, since):
        with self._conn() as c:
            return c.execute("""
                SELECT source, kind,
                       CASE WHEN qualifies THEN 'qualified'
                            WHEN cardinality(missing) > 0 THEN 'unreadable'
                            ELSE 'rejected' END AS state,
                       count(*) AS n
                FROM listings WHERE last_seen >= %s GROUP BY 1, 2, 3
            """, (since,)).fetchall()
