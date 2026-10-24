from pathlib import Path

from tracker.parse import parse_page
from tracker.robots import Robots
from tracker.vin import find_vins, is_valid_vin

FIX = Path(__file__).parent / "fixtures"
BASE = "https://bellevue.porsche.com"


def test_vin_check_digit():
    assert is_valid_vin("WP0AA2A99TS207294")
    assert not is_valid_vin("WP0AA2A98TS207294")
    assert find_vins("abc WP0AA2A99TS207294 0123456789ABCDEFG x") == ["WP0AA2A99TS207294"]


def test_cards_page():
    r = parse_page((FIX / "cards.html").read_text(), BASE)
    assert r.reported_totals == {"new": 146, "used": 92, "certified": 56}
    assert len(r.listings) == 5
    first = r.listings[0]
    assert (first.vin, first.year, first.model, first.price) == ("WP0AD2Y14TSA36058", 2026, "Taycan GTS", 176705)
    assert first.stock == "TSA36058"
    assert first.exterior == "Black" and first.interior == "Black"
    assert first.url.endswith("/details/WP0AD2Y14TSA36058")


def test_rsc_page():
    r = parse_page((FIX / "rsc.html").read_text(), BASE)
    assert r.reported_totals == {"new": 5}
    assert [l.price for l in r.listings] == [176705, 103945, 147305, 101495, 137115]
    assert r.listings[2].model == "Panamera 4"
    assert r.listings[2].exterior == "Ice Grey Metallic"


def test_monthly_payment_ignored():
    html = """<html><body><div><h3>2024 911 GT3</h3><p>WP0AC2A91RS269999</p>
    <p>$2,999/mo</p><p>$1,899 /mo</p><p>MSRP $250,000</p><p>Sale Price $239,900</p></div></body></html>"""
    r = parse_page(html, BASE)
    assert r.listings[0].price == 239900
    assert r.listings[0].msrp == 250000


def test_robots_wildcards():
    robots = Robots("""User-agent: *
Disallow: /en/inventory/porsche/*/contact
Disallow: /en/inventory/porsche/search?*sort=
Disallow: /*?*ref=
""")
    assert robots.allowed(f"{BASE}/en/inventory/porsche/search?condition=new&page=2")
    assert not robots.allowed(f"{BASE}/en/inventory/porsche/search?condition=new&sort=price")
    assert not robots.allowed(f"{BASE}/en/inventory/porsche/details/x/contact")
    assert not robots.allowed(f"{BASE}/en?ref=abc")
