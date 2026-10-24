"""SQLite storage and the logic that turns scrapes into history.

Each run compares what dealers list now with what we knew before:
- a VIN we have never seen is an arrival (first_seen = this run)
- a known VIN updates last_seen and records any price change
- an active VIN missing from complete scrapes for N runs in a row is sold, with
  the last listed price recorded as the price at time of sale
- a sold VIN that shows up again is relisted (the sale is undone)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .parse import Listing
from .scrape import DealerScrape

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vin TEXT PRIMARY KEY,
    dealer_id TEXT NOT NULL,
    condition TEXT NOT NULL,
    certified INTEGER NOT NULL DEFAULT 0,
    year INTEGER,
    model TEXT,
    exterior TEXT,
    interior TEXT,
    mileage INTEGER,
    stock TEXT,
    url TEXT,
    msrp INTEGER,
    first_price INTEGER,
    current_price INTEGER,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    baseline INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    status_text TEXT,
    missed_runs INTEGER NOT NULL DEFAULT 0,
    sold_detected_at TEXT,
    sale_price INTEGER
);
CREATE INDEX IF NOT EXISTS vehicles_dealer ON vehicles(dealer_id, status);

CREATE TABLE IF NOT EXISTS price_history (
    vin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price INTEGER,
    PRIMARY KEY (vin, observed_at)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    vin TEXT NOT NULL,
    dealer_id TEXT NOT NULL,
    type TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);

CREATE TABLE IF NOT EXISTS dealer_runs (
    run_at TEXT NOT NULL,
    dealer_id TEXT NOT NULL,
    complete INTEGER NOT NULL,
    found INTEGER NOT NULL,
    detail TEXT,
    PRIMARY KEY (run_at, dealer_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def days_between(start: str, end: str) -> float:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end)
    return (b - a).total_seconds() / 86400


def _event(conn, at, vin, dealer_id, type_, **detail):
    conn.execute(
        "INSERT INTO events (at, vin, dealer_id, type, detail) VALUES (?,?,?,?,?)",
        (at, vin, dealer_id, type_, json.dumps(detail) if detail else None),
    )


def _has_history(conn, dealer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM dealer_runs WHERE dealer_id=? AND found>0 LIMIT 1", (dealer_id,)
    ).fetchone()
    return row is not None


def _upsert(conn, run_at: str, dealer_id: str, condition: str, l: Listing, baseline: bool):
    row = conn.execute("SELECT * FROM vehicles WHERE vin=?", (l.vin,)).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO vehicles (vin, dealer_id, condition, certified, year, model, exterior,
                   interior, mileage, stock, url, msrp, first_price, current_price, first_seen,
                   last_seen, baseline, status, status_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?)""",
            (l.vin, dealer_id, condition, int(l.certified), l.year, l.model, l.exterior,
             l.interior, l.mileage, l.stock, l.url, l.msrp, l.price, l.price, run_at, run_at,
             int(baseline), l.status_text),
        )
        conn.execute("INSERT OR IGNORE INTO price_history VALUES (?,?,?)", (l.vin, run_at, l.price))
        _event(conn, run_at, l.vin, dealer_id, "baseline" if baseline else "arrived", price=l.price)
        return

    if row["status"] == "sold":
        _event(conn, run_at, l.vin, dealer_id, "relisted",
               sold_detected_at=row["sold_detected_at"], price=l.price)
    if row["dealer_id"] != dealer_id:
        _event(conn, run_at, l.vin, dealer_id, "transferred", from_dealer=row["dealer_id"])
    if l.price is not None and l.price != row["current_price"]:
        _event(conn, run_at, l.vin, dealer_id, "price_change", old=row["current_price"], new=l.price)
        conn.execute("INSERT OR IGNORE INTO price_history VALUES (?,?,?)", (l.vin, run_at, l.price))

    conn.execute(
        """UPDATE vehicles SET dealer_id=?, condition=?, certified=?,
               year=COALESCE(?, year), model=COALESCE(?, model), exterior=COALESCE(?, exterior),
               interior=COALESCE(?, interior), mileage=COALESCE(?, mileage),
               stock=COALESCE(?, stock), url=COALESCE(?, url), msrp=COALESCE(?, msrp),
               current_price=COALESCE(?, current_price), last_seen=?, status='active',
               status_text=?, missed_runs=0, sold_detected_at=NULL, sale_price=NULL
           WHERE vin=?""",
        (dealer_id, condition, int(l.certified), l.year, l.model, l.exterior, l.interior,
         l.mileage, l.stock, l.url, l.msrp, l.price, run_at, l.status_text, l.vin),
    )


def record_run(conn: sqlite3.Connection, run_at: str, scrapes: list[DealerScrape],
               missed_runs_before_sold: int, min_completeness: float) -> list[dict]:
    """Apply one run's scrapes. Returns the vehicles newly detected as sold."""
    complete: dict[str, set[str]] = {}

    # Phase 1: record everything we saw, across all dealers, so a car that moved
    # between stores is a transfer rather than a sale.
    for ds in scrapes:
        baseline = not _has_history(conn, ds.dealer_id)
        found = 0
        for cs in ds.conditions:
            for l in cs.listings:
                _upsert(conn, run_at, ds.dealer_id, cs.condition, l, baseline)
                found += 1
        is_complete = ds.is_complete(min_completeness)
        if is_complete:
            complete[ds.dealer_id] = {c.condition for c in ds.conditions}
        detail = {c.condition: {"found": len(c.listings), "reported": c.reported_total,
                                "pages": c.pages, "error": c.error} for c in ds.conditions}
        if ds.error:
            detail["error"] = ds.error
        conn.execute("INSERT OR REPLACE INTO dealer_runs VALUES (?,?,?,?,?)",
                     (run_at, ds.dealer_id, int(is_complete), found, json.dumps(detail)))

    # Phase 2: cars missing from a complete scrape of their dealer.
    sold: list[dict] = []
    for dealer_id, conditions in complete.items():
        marks = ",".join("?" * len(conditions))
        rows = conn.execute(
            f"""SELECT * FROM vehicles WHERE dealer_id=? AND status='active'
                AND last_seen < ? AND condition IN ({marks})""",
            (dealer_id, run_at, *conditions),
        ).fetchall()
        for row in rows:
            missed = row["missed_runs"] + 1
            if missed >= missed_runs_before_sold:
                conn.execute(
                    """UPDATE vehicles SET missed_runs=?, status='sold', sold_detected_at=?,
                           sale_price=current_price WHERE vin=?""",
                    (missed, run_at, row["vin"]),
                )
                _event(conn, run_at, row["vin"], dealer_id, "sold", price=row["current_price"],
                       days_on_lot=round(days_between(row["first_seen"], row["last_seen"]), 1))
                sold.append(dict(conn.execute("SELECT * FROM vehicles WHERE vin=?",
                                              (row["vin"],)).fetchone()))
            else:
                conn.execute("UPDATE vehicles SET missed_runs=? WHERE vin=?", (missed, row["vin"]))
    conn.commit()
    return sold
