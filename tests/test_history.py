from tracker import db
from tracker.parse import Listing
from tracker.scrape import ConditionScrape, DealerScrape
from tracker.notify import build_email, matches

A, B, C = "WP0AD2Y14TSA36058", "WP1AG2A59TLB40863", "WP0AA2YA8TL001541"


def scrape(dealer, cars, reported=None, error=None):
    listings = [Listing(vin=v, year=2026, model=m, price=p) for v, m, p in cars]
    cs = ConditionScrape("new", listings, reported if reported is not None else len(listings), 1, error)
    return DealerScrape(dealer, [cs])


def run(conn, at, *scrapes):
    return db.record_run(conn, at, list(scrapes), missed_runs_before_sold=2, min_completeness=0.9)


def test_lifecycle(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    t = lambda d: f"2026-10-{d:02d}T12:00:00+00:00"
    assert run(conn, t(1), scrape("bellevue", [(A, "Taycan GTS", 176705), (B, "Macan S", 103945)])) == []
    # Day 5: C arrives, A drops in price.
    run(conn, t(5), scrape("bellevue", [(A, "Taycan GTS", 169900), (B, "Macan S", 103945), (C, "Panamera 4", 147305)]))
    # Day 10: A missing once -> not sold yet.
    assert run(conn, t(10), scrape("bellevue", [(B, "Macan S", 103945), (C, "Panamera 4", 147305)])) == []
    # Day 11: incomplete scrape (error) must not count as a miss.
    assert run(conn, t(11), scrape("bellevue", [(B, "Macan S", 103945)], error="timeout")) == []
    # Day 12: second complete miss -> sold at last listed price.
    sold = run(conn, t(12), scrape("bellevue", [(B, "Macan S", 103945), (C, "Panamera 4", 147305)]))
    assert [s["vin"] for s in sold] == [A]
    assert sold[0]["sale_price"] == 169900
    assert sold[0]["first_price"] == 176705
    assert sold[0]["baseline"] == 1  # was already there when tracking started
    assert round(db.days_between(sold[0]["first_seen"], sold[0]["last_seen"])) == 4
    c = conn.execute("SELECT baseline, first_seen FROM vehicles WHERE vin=?", (C,)).fetchone()
    assert c["baseline"] == 0 and c["first_seen"] == t(5)
    types = [r["type"] for r in conn.execute("SELECT type FROM events WHERE vin=? ORDER BY id", (A,))]
    assert types == ["baseline", "price_change", "sold"]


def test_incomplete_scrape_never_sells(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    run(conn, "2026-10-01T00:00:00+00:00", scrape("bend", [(A, "x", 1000), (B, "y", 1000)]))
    for d in range(2, 6):
        # Site says 2 cars but we only parsed 1: below 90%, so incomplete.
        assert run(conn, f"2026-10-0{d}T00:00:00+00:00", scrape("bend", [(B, "y", 1000)], reported=2)) == []
    assert conn.execute("SELECT status FROM vehicles WHERE vin=?", (A,)).fetchone()[0] == "active"


def test_transfer_is_not_a_sale(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    run(conn, "2026-10-01T00:00:00+00:00", scrape("bend", [(A, "x", 1000)]), scrape("boise", [(B, "y", 1000)]))
    for d in (2, 3):
        assert run(conn, f"2026-10-0{d}T00:00:00+00:00",
                   scrape("bend", []), scrape("boise", [(B, "y", 1000), (A, "x", 1000)])) == []
    row = conn.execute("SELECT dealer_id, status FROM vehicles WHERE vin=?", (A,)).fetchone()
    assert tuple(row) == ("boise", "active")


def test_relisted(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    s_all = scrape("bend", [(A, "x", 1000), (B, "y", 1000)])
    s_b = lambda: scrape("bend", [(B, "y", 1000)])
    run(conn, "2026-10-01T00:00:00+00:00", s_all)
    run(conn, "2026-10-02T00:00:00+00:00", s_b())
    assert len(run(conn, "2026-10-03T00:00:00+00:00", s_b())) == 1
    run(conn, "2026-10-04T00:00:00+00:00", scrape("bend", [(A, "x", 1000), (B, "y", 1000)]))
    row = conn.execute("SELECT status, sale_price FROM vehicles WHERE vin=?", (A,)).fetchone()
    assert tuple(row) == ("active", None)


def test_subscriber_filters_and_email():
    car = {"vin": A, "dealer_id": "bellevue", "condition": "used", "certified": 1, "year": 2022,
           "model": "911 GT3 Touring", "sale_price": 239900, "first_price": 249900,
           "first_seen": "2026-09-01T00:00:00+00:00", "last_seen": "2026-10-01T00:00:00+00:00",
           "baseline": 0, "mileage": 4200, "url": "https://x/y"}
    assert matches({"filters": {"models": ["gt3"]}}, car)
    assert not matches({"filters": {"models": ["Taycan"]}}, car)
    assert not matches({"filters": {"dealers": ["boise"]}}, car)
    assert not matches({"filters": {"max_price": 200000}}, car)
    subject, text, body = build_email([car], {"bellevue": {"name": "Porsche Bellevue"}}, "https://dash")
    assert subject == "Sold: 2022 911 GT3 Touring at Porsche Bellevue ($239,900)"
    assert "30 days" in text and "$249,900" in body
