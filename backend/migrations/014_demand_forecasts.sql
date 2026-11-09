CREATE TABLE demand_forecasts (
    area TEXT NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    source_issued_at TIMESTAMPTZ NOT NULL,
    load_mw DOUBLE PRECISION NOT NULL CHECK (load_mw >= 0 AND load_mw < 'Infinity'),
    source TEXT NOT NULL,
    PRIMARY KEY (area, ts, issued_at, source)
);
