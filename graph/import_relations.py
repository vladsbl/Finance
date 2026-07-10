#!/usr/bin/env python3
"""Import known ticker relations from a CSV seed into the ``relations`` table.

Reads data/relations_seed.csv and upserts every row into SQLite. The upsert key
is (source_ticker, relation_type, target_name), so re-running is idempotent:
existing rows have their target_ticker / notes refreshed, new rows are added,
and no duplicates are created.

Run directly:
    python graph/import_relations.py
"""

import csv
import logging
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DB_PATH = os.path.join(REPO_ROOT, "data", "marketdb.db")
CSV_PATH = os.path.join(REPO_ROOT, "data", "relations_seed.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_relations")


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS relations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ticker TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    target_name   TEXT NOT NULL,
    target_ticker TEXT,
    notes         TEXT,
    UNIQUE (source_ticker, relation_type, target_name)
);
"""

UPSERT_SQL = """
INSERT INTO relations
    (source_ticker, relation_type, target_name, target_ticker, notes)
VALUES
    (:source_ticker, :relation_type, :target_name, :target_ticker, :notes)
ON CONFLICT(source_ticker, relation_type, target_name) DO UPDATE SET
    target_ticker = excluded.target_ticker,
    notes         = excluded.notes;
"""

REQUIRED_COLUMNS = {"source_ticker", "relation_type", "target_name",
                    "target_ticker", "notes"}


def _clean(value):
    return (value or "").strip()


def read_rows(csv_path):
    """Yield cleaned relation dicts from the seed CSV."""
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")

        for line_no, row in enumerate(reader, start=2):
            source = _clean(row.get("source_ticker"))
            rel = _clean(row.get("relation_type"))
            target = _clean(row.get("target_name"))
            if not (source and rel and target):
                logger.warning("Line %d skipped (missing source/type/target).", line_no)
                continue
            ticker = _clean(row.get("target_ticker"))
            yield {
                "source_ticker": source,
                "relation_type": rel,
                "target_name": target,
                # Empty target_ticker means an external (untracked) entity.
                "target_ticker": ticker or None,
                "notes": _clean(row.get("notes")) or None,
            }


def main():
    if not os.path.exists(CSV_PATH):
        logger.error("Seed CSV not found at %s.", CSV_PATH)
        return 1

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Database error: %s", exc)
        return 1

    try:
        rows = list(read_rows(CSV_PATH))
    except (OSError, ValueError) as exc:
        logger.error("Could not read %s: %s", CSV_PATH, exc)
        conn.close()
        return 1

    if not rows:
        logger.warning("No valid rows in %s.", CSV_PATH)
        conn.close()
        return 1

    before = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    try:
        conn.executemany(UPSERT_SQL, rows)
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        logger.error("Upsert failed: %s", exc)
        conn.close()
        return 1
    after = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

    external = sum(1 for r in rows if not r["target_ticker"])
    logger.info("Imported %d rows (%d new, %d updated). External targets: %d.",
                len(rows), after - before, len(rows) - (after - before), external)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
