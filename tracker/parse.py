"""Extract vehicle listings from a Porsche dealer inventory search page.

Porsche dealer sites are server-rendered Next.js pages. We don't depend on any
one page structure; instead we combine two independent strategies and merge:

1. Structured data: JSON-LD, __NEXT_DATA__, and the React Server Components
   payload (`self.__next_f.push`). Any JSON object holding a valid VIN is
   treated as a vehicle and its fields are mapped by key name.
2. Rendered cards: for each VIN in the visible HTML, find the largest element
   around it that holds no other VIN (the listing card) and read the year,
   model, price, mileage, and stock number from its text.

Structured fields win; card fields fill the gaps.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from .vin import VIN_RE, find_vins, is_valid_vin

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d{2})?")
MONTHLY_RE = re.compile(r"^\s*(?:/\s*mo|per\s+month|/month|mo\.)", re.I)
MILES_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)\s*(?:mi\b|miles\b)", re.I)
STOCK_RE = re.compile(r"Stock(?:\s*(?:No\.?|Number|#))?\s*:?\s*([A-Z0-9][A-Z0-9-]{3,})", re.I)
YEAR_MODEL_RE = re.compile(r"^\s*((?:19|20)\d{2})\s+(?:Porsche\s+)?(.+?)\s*$")
YEAR_ONLY_RE = re.compile(r"^\s*((?:19|20)\d{2})\s*$")
EXT_RE = re.compile(r"Exterior(?:\s+colou?r)?\s*:?\s*(.+)", re.I)
INT_RE = re.compile(r"Interior(?:\s+colou?r)?\s*:?\s*(.+)", re.I)
NEXT_F_RE = re.compile(r"self\.__next_f\.push\(\[\s*1\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\]\)", re.S)
STATUS_WORDS = ("in transit", "reserved", "sale pending", "pending sale", "sold", "on hold")


@dataclass
class Listing:
    vin: str
    year: int | None = None
    model: str | None = None
    price: int | None = None
    msrp: int | None = None
    mileage: int | None = None
    stock: str | None = None
    exterior: str | None = None
    interior: str | None = None
    url: str | None = None
    certified: bool = False
    status_text: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageResult:
    listings: list[Listing]
    reported_totals: dict[str, int]


# ---------------------------------------------------------------- helpers

def _to_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, dict):
        for k in ("value", "amount", "price", "raw"):
            if k in value:
                return _to_int(value[k])
        return None
    if isinstance(value, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", value)
        if m:
            try:
                return int(float(m.group(0).replace(",", "")))
            except ValueError:
                return None
    return None


def _to_str(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = " ".join(value.split())
        return s or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for k in ("name", "label", "value", "text", "description"):
            if isinstance(value.get(k), str):
                return _to_str(value[k])
    return None


def _clean_model(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"^\s*(?:New|Used|Pre-Owned|Certified Pre-Owned)\s+", "", s, flags=re.I)
    s = re.sub(r"^\s*(?:19|20)\d{2}\s+", "", s)
    s = re.sub(r"^\s*Porsche\s+", "", s, flags=re.I)
    return s.strip() or None


# ------------------------------------------------------- structured data

_KEYS = {
    "vin": ("vin", "vehicleidentificationnumber"),
    "year": ("modelyear", "year", "vehiclemodeldate"),
    "model": ("modelname", "modeldescription", "model", "title", "name", "vehiclename", "displayname"),
    "price": ("saleprice", "sellingprice", "internetprice", "askingprice", "finalprice",
              "listprice", "price", "retailprice", "pricevalue", "totalprice"),
    "msrp": ("msrp", "totalmsrp", "baseprice"),
    "mileage": ("mileage", "mileagefromodometer", "odometer", "miles"),
    "stock": ("stocknumber", "stockno", "stocknum", "stock", "sku"),
    "exterior": ("exteriorcolor", "exteriorcolour", "colorexterior", "color", "exteriorcolorname"),
    "interior": ("interiorcolor", "interiorcolour", "colorinterior", "interiorcolorname"),
    "url": ("url", "detailurl", "detailsurl", "vdpurl", "href", "link"),
    "condition": ("condition", "itemcondition", "vehiclecondition"),
}


def _flatten(d: dict, depth: int = 2, prefix: str = "") -> dict[str, object]:
    """Lower-cased, punctuation-free keys, with nested dicts merged one level down.

    Top-level keys take precedence over nested ones of the same name.
    """
    out: dict[str, object] = {}
    nested: list[tuple[str, dict]] = []
    for k, v in d.items():
        key = re.sub(r"[^a-z]", "", str(k).lower())
        if isinstance(v, dict) and depth > 0:
            nested.append((key, v))
        out.setdefault(key, v)
    for parent, v in nested:
        for k2, v2 in _flatten(v, depth - 1).items():
            out.setdefault(k2, v2)
            out.setdefault(parent + k2, v2)
    return out


def _pick(flat: dict, field_name: str):
    for k in _KEYS[field_name]:
        if k in flat and flat[k] not in (None, "", [], {}):
            return flat[k]
    return None


def _listing_from_obj(obj: dict, base_url: str) -> Listing | None:
    flat = _flatten(obj)
    vin = _to_str(_pick(flat, "vin"))
    if not vin or not is_valid_vin(vin.upper()):
        return None
    lst = Listing(vin=vin.upper(), sources=["json"])
    year = _to_int(_pick(flat, "year"))
    lst.year = year if year and 1950 <= year <= 2100 else None
    lst.model = _clean_model(_to_str(_pick(flat, "model")))
    if lst.model and VIN_RE.fullmatch(lst.model):
        lst.model = None
    price = _to_int(_pick(flat, "price"))
    lst.price = price if price and price >= 1000 else None
    msrp = _to_int(_pick(flat, "msrp"))
    lst.msrp = msrp if msrp and msrp >= 1000 else None
    lst.mileage = _to_int(_pick(flat, "mileage"))
    lst.stock = _to_str(_pick(flat, "stock"))
    lst.exterior = _to_str(_pick(flat, "exterior"))
    lst.interior = _to_str(_pick(flat, "interior"))
    url = _to_str(_pick(flat, "url"))
    if url and (url.startswith("/") or url.startswith("http")):
        lst.url = urljoin(base_url, url)
    cond = (_to_str(_pick(flat, "condition")) or "").lower()
    if "approved" in cond or "certified" in cond:
        lst.certified = True
    return lst


def _walk_json(node, found: list[dict]) -> None:
    if isinstance(node, dict):
        has_vin = any(
            re.sub(r"[^a-z]", "", str(k).lower()) in _KEYS["vin"] and isinstance(v, str)
            for k, v in node.items()
        )
        if has_vin:
            found.append(node)
        for v in node.values():
            _walk_json(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_json(v, found)


def _json_blobs(html: str, soup: BeautifulSoup) -> list:
    blobs = []
    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        text = script.string or script.get_text() or ""
        if stype == "application/ld+json" or script.get("id") == "__NEXT_DATA__":
            try:
                blobs.append(json.loads(text))
            except (ValueError, TypeError):
                pass
    # React Server Components flight data: concatenate pushed string chunks,
    # then parse each "<id>:<json>" row.
    chunks = []
    for m in NEXT_F_RE.finditer(html):
        try:
            chunks.append(json.loads(m.group(1)))
        except ValueError:
            continue
    flight = "".join(chunks)
    for row in flight.split("\n"):
        m = re.match(r"^[0-9a-zA-Z]+:([A-Z]?)(.*)$", row)
        if not m:
            continue
        payload = m.group(2)
        if not payload or payload[0] not in "[{":
            continue
        try:
            blobs.append(json.loads(payload))
        except ValueError:
            continue
    return blobs


def parse_structured(html: str, soup: BeautifulSoup, base_url: str) -> dict[str, Listing]:
    found: list[dict] = []
    for blob in _json_blobs(html, soup):
        _walk_json(blob, found)
    out: dict[str, Listing] = {}
    for obj in found:
        lst = _listing_from_obj(obj, base_url)
        if not lst:
            continue
        if lst.vin in out:
            _merge(out[lst.vin], lst)
        else:
            out[lst.vin] = lst
    return out


# ---------------------------------------------------------- rendered cards

def _card_for(node: NavigableString) -> Tag | None:
    """Largest ancestor of `node` whose text contains exactly one VIN."""
    best = None
    el = node.parent
    while isinstance(el, Tag) and el.name not in ("body", "html", "[document]"):
        if len(find_vins(el.get_text(" "))) > 1:
            break
        best = el
        el = el.parent
    return best


def _lines(card: Tag) -> list[str]:
    return [" ".join(s.split()) for s in card.get_text("\n").split("\n") if s.strip()]


def _price_from_lines(lines: list[str]) -> tuple[int | None, int | None]:
    """(price, msrp) from a card's text lines, ignoring monthly payments."""
    labelled_price = None
    msrp = None
    others: list[int] = []
    for i, line in enumerate(lines):
        for m in PRICE_RE.finditer(line):
            if MONTHLY_RE.match(line[m.end():]):
                continue
            value = int(m.group(1).replace(",", ""))
            if value < 5000:
                continue
            # The label is the text before the price on its line, or the
            # previous line when the price sits on a line of its own.
            label = line[:m.start()].strip()
            if not re.search(r"[A-Za-z]", label) and i:
                label = lines[i - 1]
            low = label.lower()
            if "msrp" in low:
                msrp = msrp or value
            elif "price" in low and labelled_price is None:
                labelled_price = value
            else:
                others.append(value)
    price = labelled_price or (min(others) if others else None) or msrp
    return price, msrp


def parse_cards(soup: BeautifulSoup, base_url: str) -> dict[str, Listing]:
    out: dict[str, Listing] = {}
    body = soup.body or soup
    for text_node in body.find_all(string=VIN_RE):
        if isinstance(text_node.parent, Tag) and text_node.parent.name in ("script", "style", "noscript"):
            continue
        vins = find_vins(str(text_node))
        if len(vins) != 1 or vins[0] in out:
            continue
        vin = vins[0]
        card = _card_for(text_node)
        if card is None:
            continue
        lines = _lines(card)
        lst = Listing(vin=vin, sources=["card"])

        heading = card.find(["h1", "h2", "h3", "h4"])
        candidates = ([" ".join(heading.get_text(" ").split())] if heading else []) + lines
        for i, line in enumerate(candidates):
            m = YEAR_MODEL_RE.match(line)
            if m and not PRICE_RE.search(line) and len(line) < 120:
                lst.year, lst.model = int(m.group(1)), _clean_model(m.group(2))
                break
            m = YEAR_ONLY_RE.match(line)
            if m and i + 1 < len(candidates):
                lst.year, lst.model = int(m.group(1)), _clean_model(candidates[i + 1])
                break
        if not lst.model and heading:
            lst.model = _clean_model(heading.get_text(" "))

        lst.price, lst.msrp = _price_from_lines(lines)
        text = "\n".join(lines)
        if m := MILES_RE.search(text):
            lst.mileage = int(m.group(1).replace(",", ""))
        if m := STOCK_RE.search(text):
            lst.stock = m.group(1)
        for line in lines:
            if not lst.exterior and (m := EXT_RE.match(line)):
                lst.exterior = m.group(1).strip() or None
            if not lst.interior and (m := INT_RE.match(line)):
                lst.interior = m.group(1).strip() or None
        low = text.lower()
        lst.certified = "porsche approved" in low or "certified" in low
        for word in STATUS_WORDS:
            if re.search(r"\b" + word + r"\b", low):
                lst.status_text = word
                break
        for a in card.find_all("a", href=True):
            href = a["href"]
            if vin in href or "/inventory/" in href:
                lst.url = urljoin(base_url, href)
                break
        if not lst.url and card.name == "a" and card.get("href"):
            lst.url = urljoin(base_url, card["href"])
        if lst.url is None:
            parent_link = card.find_parent("a", href=True)
            if parent_link:
                lst.url = urljoin(base_url, parent_link["href"])
        out[vin] = lst
    return out


# ------------------------------------------------------------------ merge

def _merge(primary: Listing, extra: Listing) -> Listing:
    for name in ("year", "model", "price", "msrp", "mileage", "stock",
                 "exterior", "interior", "url", "status_text"):
        if getattr(primary, name) in (None, "") and getattr(extra, name) not in (None, ""):
            setattr(primary, name, getattr(extra, name))
    primary.certified = primary.certified or extra.certified
    for s in extra.sources:
        if s not in primary.sources:
            primary.sources.append(s)
    return primary


def reported_totals(soup: BeautifulSoup) -> dict[str, int]:
    """Totals the site shows in its condition filter, e.g. "New (146)"."""
    text = " ".join(soup.get_text(" ").split())
    totals: dict[str, int] = {}
    if m := re.search(r"\bNew\s*\(\s*(\d+)\s*\)", text):
        totals["new"] = int(m.group(1))
    for m in re.finditer(r"(Certified\s+)?Pre-?Owned\s*\(\s*(\d+)\s*\)", text, re.I):
        if not m.group(1):
            totals.setdefault("used", int(m.group(2)))
        else:
            totals.setdefault("certified", int(m.group(2)))
    return totals


def parse_page(html: str, base_url: str) -> PageResult:
    soup = BeautifulSoup(html, "lxml")
    structured = parse_structured(html, soup, base_url)
    cards = parse_cards(soup, base_url)
    merged: dict[str, Listing] = {}
    # Keep page order from the rendered cards when we have them.
    for vin in list(cards) + [v for v in structured if v not in cards]:
        if vin in structured and vin in cards:
            merged[vin] = _merge(structured[vin], cards[vin])
        else:
            merged[vin] = structured.get(vin) or cards[vin]
    return PageResult(listings=list(merged.values()), reported_totals=reported_totals(soup))
