CREATE TABLE IF NOT EXISTS log_table (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    log_airnet TEXT NOT NULL,
    log_npaw TEXT NOT NULL,
    log_onesense TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_table_created_at ON log_table(created_at);
