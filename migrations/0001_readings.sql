-- UTC timestamps are normalized by the API before insertion.
CREATE TABLE IF NOT EXISTS readings (
    site_id TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    energy_kwh REAL NOT NULL CHECK (energy_kwh >= 0 AND energy_kwh <= 1000000),
    PRIMARY KEY (site_id, interval_start)
);
