"""Fetch every inventory page for a dealer, politely."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import requests

from .parse import Listing, parse_page
from .robots import Robots

log = logging.getLogger(__name__)

USER_AGENT = os.environ.get(
    "TRACKER_USER_AGENT",
    "Mozilla/5.0 (compatible; PorscheNWInventoryTracker/1.0; personal, non-commercial; "
    "checks each dealer a few times a day)",
)


@dataclass
class ConditionScrape:
    condition: str
    listings: list[Listing] = field(default_factory=list)
    reported_total: int | None = None
    pages: int = 0
    error: str | None = None

    def completeness(self) -> float | None:
        if self.reported_total in (None, 0):
            return None
        return len(self.listings) / self.reported_total


@dataclass
class DealerScrape:
    dealer_id: str
    conditions: list[ConditionScrape] = field(default_factory=list)
    error: str | None = None

    def is_complete(self, min_completeness: float) -> bool:
        if self.error or not self.conditions:
            return False
        for c in self.conditions:
            if c.error:
                return False
            ratio = c.completeness()
            if ratio is not None and ratio < min_completeness:
                return False
            if ratio is None and not c.listings:
                # No total shown and nothing found: can't tell empty from broken.
                return False
        return True


class Fetcher:
    def __init__(self, delay: float, save_dir: Path | None = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.delay = delay
        self.save_dir = save_dir
        self._robots: dict[str, Robots] = {}
        self._last: dict[str, float] = {}

    def robots(self, base_url: str) -> Robots:
        if base_url not in self._robots:
            try:
                r = self.session.get(base_url.rstrip("/") + "/robots.txt", timeout=20)
                text = r.text if r.ok else ""
            except requests.RequestException:
                text = ""
            self._robots[base_url] = Robots(text, USER_AGENT)
        return self._robots[base_url]

    def get(self, base_url: str, url: str) -> str:
        if not self.robots(base_url).allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        wait = self._last.get(base_url, 0) + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last_exc = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=45)
                self._last[base_url] = time.monotonic()
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r.text
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self.delay * (attempt + 2))
        raise RuntimeError(f"failed to fetch {url}: {last_exc}")


def search_url(base_url: str, condition: str, page: int) -> str:
    params = {"condition": condition}
    if page > 1:
        params["page"] = str(page)
    return f"{base_url.rstrip('/')}/en/inventory/porsche/search?{urlencode(params)}"


def scrape_dealer(fetcher: Fetcher, dealer: dict, conditions: list[str], max_pages: int) -> DealerScrape:
    result = DealerScrape(dealer_id=dealer["id"])
    base = dealer["base_url"]
    for condition in conditions:
        cs = ConditionScrape(condition=condition)
        result.conditions.append(cs)
        seen: dict[str, Listing] = {}
        try:
            for page in range(1, max_pages + 1):
                url = search_url(base, condition, page)
                html = fetcher.get(base, url)
                cs.pages = page
                if fetcher.save_dir and page == 1:
                    fetcher.save_dir.mkdir(parents=True, exist_ok=True)
                    (fetcher.save_dir / f"{dealer['id']}-{condition}-p1.html").write_text(html)
                parsed = parse_page(html, base)
                if page == 1:
                    cs.reported_total = parsed.reported_totals.get(condition)
                new = [l for l in parsed.listings if l.vin not in seen]
                for l in new:
                    seen[l.vin] = l
                log.info("%s %s page %d: %d listings (%d new)", dealer["id"], condition,
                         page, len(parsed.listings), len(new))
                if not new:
                    break
                if cs.reported_total is not None and len(seen) >= cs.reported_total:
                    break
        except Exception as exc:  # noqa: BLE001 - one dealer failing must not stop the run
            cs.error = str(exc)
            log.warning("%s %s failed: %s", dealer["id"], condition, exc)
        cs.listings = list(seen.values())
    return result
