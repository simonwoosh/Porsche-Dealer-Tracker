"""Build the static dashboard: web/index.html plus a data.json snapshot."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from .db import days_between, now_iso

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def export(conn: sqlite3.Connection, dealers: list[dict]) -> dict:
    generated = now_iso()
    history: dict[str, list] = {}
    for r in conn.execute("SELECT vin, observed_at, price FROM price_history ORDER BY observed_at"):
        history.setdefault(r["vin"], []).append([r["observed_at"], r["price"]])

    vehicles = []
    for r in conn.execute("SELECT * FROM vehicles"):
        v = dict(r)
        end = v["last_seen"] if v["status"] == "sold" else generated
        v["days_on_lot"] = round(days_between(v["first_seen"], end), 1)
        h = history.get(v["vin"], [])
        v["price_history"] = h if len(h) > 1 else []
        vehicles.append(v)

    latest_runs = {}
    for r in conn.execute(
        """SELECT d.* FROM dealer_runs d JOIN (SELECT dealer_id, MAX(run_at) m FROM dealer_runs
           GROUP BY dealer_id) x ON d.dealer_id=x.dealer_id AND d.run_at=x.m"""
    ):
        latest_runs[r["dealer_id"]] = {"run_at": r["run_at"], "complete": bool(r["complete"]),
                                       "found": r["found"], "detail": json.loads(r["detail"] or "{}")}
    first_run = conn.execute("SELECT MIN(run_at) FROM dealer_runs").fetchone()[0]

    events = [dict(r) | {"detail": json.loads(r["detail"]) if r["detail"] else {}}
              for r in conn.execute(
                  "SELECT * FROM events WHERE type != 'baseline' ORDER BY at DESC, id DESC LIMIT 500")]

    return {
        "generated_at": generated,
        "tracking_since": first_run,
        "dealers": [{k: d.get(k) for k in ("id", "name", "city", "state", "base_url")}
                    | {"last_run": latest_runs.get(d["id"])} for d in dealers],
        "vehicles": vehicles,
        "events": events,
    }


def build(conn: sqlite3.Connection, dealers: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(WEB_DIR / "index.html", out_dir / "index.html")
    (out_dir / "data.json").write_text(json.dumps(export(conn, dealers), separators=(",", ":")))
    return out_dir
