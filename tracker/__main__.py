"""Command line entry point.

    python -m tracker run            scrape all dealers, update the database, send alerts
    python -m tracker site           rebuild the dashboard from the database
    python -m tracker test-alert     send a sample sold alert to every subscriber
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from . import db, notify, site
from .scrape import Fetcher, scrape_dealer

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_subscribers() -> list[dict]:
    raw = os.environ.get("SUBSCRIBERS_YAML")
    if raw and raw.strip():
        data = yaml.safe_load(raw)
    elif (ROOT / "subscribers.yaml").exists():
        data = yaml.safe_load((ROOT / "subscribers.yaml").read_text())
    else:
        return []
    return (data or {}).get("subscribers") or []


def cmd_run(args, cfg) -> int:
    dealers = [d for d in cfg["dealers"] if d.get("enabled", True)]
    if args.dealer:
        dealers = [d for d in dealers if d["id"] in args.dealer]
    sc = cfg.get("scrape", {})
    fetcher = Fetcher(delay=sc.get("delay_seconds", 4),
                      save_dir=Path(args.save_html) if args.save_html else None)
    scrapes = []
    for d in dealers:
        logging.info("scraping %s", d["name"])
        scrapes.append(scrape_dealer(fetcher, d, cfg.get("conditions", ["new", "used"]),
                                     sc.get("max_pages", 40)))

    conn = db.connect(args.db)
    run_at = db.now_iso()
    sold = db.record_run(conn, run_at, scrapes, sc.get("missed_runs_before_sold", 2),
                         sc.get("min_completeness", 0.9))

    print(f"\nRun {run_at}")
    for s in scrapes:
        parts = [f"{c.condition}: {len(c.listings)}/{c.reported_total if c.reported_total is not None else '?'}"
                 + (f" ERROR {c.error}" if c.error else "") for c in s.conditions]
        status = "complete" if s.is_complete(sc.get("min_completeness", 0.9)) else "INCOMPLETE"
        print(f"  {s.dealer_id:14} {status:10} " + "  ".join(parts))
    print(f"  sold this run: {len(sold)}")

    by_id = {d["id"]: d for d in cfg["dealers"]}
    notify.notify_sold(sold, load_subscribers(), by_id, os.environ.get("DASHBOARD_URL"),
                       dry_run=args.no_alerts)
    site.build(conn, cfg["dealers"], Path(args.site_dir))

    total_found = sum(len(c.listings) for s in scrapes for c in s.conditions)
    if args.strict and total_found == 0:
        print("No vehicles found at any dealer; the parser may need attention.", file=sys.stderr)
        return 2
    return 0


def cmd_site(args, cfg) -> int:
    conn = db.connect(args.db)
    out = site.build(conn, cfg["dealers"], Path(args.site_dir))
    print(f"dashboard written to {out}")
    return 0


def cmd_test_alert(args, cfg) -> int:
    sample = {
        "vin": "WP0AA2A99TS207294", "dealer_id": cfg["dealers"][0]["id"], "condition": "new",
        "certified": 0, "year": 2026, "model": "911 Carrera (test alert)", "sale_price": 181575,
        "first_price": 184900, "first_seen": "2026-09-01T12:00:00+00:00",
        "last_seen": "2026-10-20T12:00:00+00:00", "baseline": 0, "mileage": 12, "url": "",
    }
    subs = load_subscribers()
    if not subs:
        print("No subscribers configured (SUBSCRIBERS_YAML secret or subscribers.yaml).")
        return 1
    for sub in subs:
        sub = dict(sub, filters={})
        notify.notify_sold([sample], [sub], {d["id"]: d for d in cfg["dealers"]},
                           os.environ.get("DASHBOARD_URL"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracker")
    p.add_argument("--config", default=str(ROOT / "dealers.yaml"))
    p.add_argument("--db", default=str(ROOT / "data" / "inventory.db"))
    p.add_argument("--site-dir", default=str(ROOT / "site"))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dealer", action="append", help="only this dealer id (repeatable)")
    r.add_argument("--save-html", metavar="DIR", help="save page 1 HTML per dealer for debugging")
    r.add_argument("--no-alerts", action="store_true", help="don't send alerts, just log them")
    r.add_argument("--strict", action="store_true", help="exit non-zero if nothing was found")
    sub.add_parser("site")
    sub.add_parser("test-alert")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    return {"run": cmd_run, "site": cmd_site, "test-alert": cmd_test_alert}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
