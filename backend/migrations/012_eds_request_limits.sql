CREATE TABLE eds_request_limits (
    dataset TEXT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL
);
