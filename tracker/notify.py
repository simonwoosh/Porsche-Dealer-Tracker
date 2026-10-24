"""Sold-car alerts by email (SMTP) and optional phone push (ntfy.sh)."""
from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

import requests

from .db import days_between

log = logging.getLogger(__name__)


def matches(sub: dict, car: dict) -> bool:
    f = sub.get("filters") or {}
    if f.get("dealers") and car["dealer_id"] not in f["dealers"]:
        return False
    if f.get("conditions") and car["condition"] not in f["conditions"]:
        return False
    if f.get("models"):
        model = (car.get("model") or "").lower()
        if not any(str(m).lower() in model for m in f["models"]):
            return False
    price = car.get("sale_price")
    if f.get("min_price") and (price is None or price < f["min_price"]):
        return False
    if f.get("max_price") and (price is None or price > f["max_price"]):
        return False
    return True


def money(v) -> str:
    return f"${v:,.0f}" if v is not None else "n/a"


def describe(car: dict, dealers: dict[str, dict]) -> dict:
    d = dealers.get(car["dealer_id"], {})
    days = days_between(car["first_seen"], car["last_seen"])
    on_lot = f"{days:.0f} days" if days >= 1 else "under a day"
    if car.get("baseline"):
        on_lot = "at least " + on_lot
    title = " ".join(str(x) for x in (car.get("year"), car.get("model")) if x) or car["vin"]
    cond = "CPO" if car.get("certified") else car["condition"]
    return {
        "title": title,
        "dealer": d.get("name", car["dealer_id"]),
        "condition": cond,
        "price": money(car.get("sale_price")),
        "first_price": money(car.get("first_price")),
        "on_lot": on_lot,
        "vin": car["vin"],
        "url": car.get("url") or "",
        "mileage": f"{car['mileage']:,} mi" if car.get("mileage") else "",
    }


def build_email(cars: list[dict], dealers: dict[str, dict], dashboard_url: str | None) -> tuple[str, str, str]:
    items = [describe(c, dealers) for c in cars]
    if len(items) == 1:
        subject = f"Sold: {items[0]['title']} at {items[0]['dealer']} ({items[0]['price']})"
    else:
        subject = f"{len(items)} Porsches sold at Northwest dealers"
    text_lines = []
    rows = []
    for i in items:
        text_lines.append(
            f"- {i['title']} ({i['condition']}) at {i['dealer']}: listed at {i['price']} when it sold, "
            f"first listed at {i['first_price']}, on the lot {i['on_lot']}. VIN {i['vin']} {i['url']}"
        )
        link = f'<a href="{html.escape(i["url"])}">{html.escape(i["title"])}</a>' if i["url"] else html.escape(i["title"])
        rows.append(
            "<tr>" + "".join(
                f'<td style="padding:6px 10px;border-bottom:1px solid #ddd">{v}</td>'
                for v in (link, html.escape(i["dealer"]), html.escape(i["condition"]), i["price"],
                          i["first_price"], html.escape(i["on_lot"]), html.escape(i["mileage"]),
                          f'<code>{i["vin"]}</code>')
            ) + "</tr>"
        )
    footer = f"\n\nDashboard: {dashboard_url}" if dashboard_url else ""
    text = "These cars were removed from dealer inventory and are presumed sold:\n\n" + "\n".join(text_lines) + footer
    head = "".join(f'<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #333">{h}</th>'
                   for h in ("Car", "Dealer", "Type", "Price at sale", "First listed", "Days on lot", "Miles", "VIN"))
    dash = f'<p><a href="{html.escape(dashboard_url)}">Open the dashboard</a></p>' if dashboard_url else ""
    body = (
        '<div style="font-family:system-ui,sans-serif;font-size:14px">'
        "<p>These cars were removed from dealer inventory and are presumed sold:</p>"
        f'<table style="border-collapse:collapse"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        f"{dash}</div>"
    )
    return subject, text, body


def send_email(to: str, subject: str, text: str, html_body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM") or user
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)


def send_ntfy(topic: str, cars: list[dict], dealers: dict[str, dict], dashboard_url: str | None) -> None:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    for car in cars:
        i = describe(car, dealers)
        headers = {"Title": f"Sold: {i['title']}", "Tags": "racing_car"}
        if i["url"] or dashboard_url:
            headers["Click"] = i["url"] or dashboard_url
        body = f"{i['dealer']} · {i['price']} at sale · {i['on_lot']} on lot"
        requests.post(f"{server}/{topic}", data=body.encode(), headers=headers, timeout=20)


def notify_sold(cars: list[dict], subscribers: list[dict], dealers: dict[str, dict],
                dashboard_url: str | None = None, dry_run: bool = False) -> list[str]:
    """Send alerts; returns a log of what was sent (or would be, on a dry run)."""
    sent = []
    if not cars:
        return sent
    smtp_ready = bool(os.environ.get("SMTP_HOST"))
    for sub in subscribers:
        mine = [c for c in cars if matches(sub, c)]
        if not mine:
            continue
        name = sub.get("name") or sub.get("email") or sub.get("ntfy_topic")
        if sub.get("email"):
            subject, text, body = build_email(mine, dealers, dashboard_url)
            if dry_run or not smtp_ready:
                sent.append(f"[email skipped{' (no SMTP_HOST)' if not smtp_ready else ''}] {name}: {subject}")
            else:
                try:
                    send_email(sub["email"], subject, text, body)
                    sent.append(f"[email] {name}: {subject}")
                except Exception as exc:  # noqa: BLE001
                    log.error("email to %s failed: %s", name, exc)
        if sub.get("ntfy_topic"):
            if dry_run:
                sent.append(f"[ntfy skipped] {name}: {len(mine)} cars")
            else:
                try:
                    send_ntfy(sub["ntfy_topic"], mine, dealers, dashboard_url)
                    sent.append(f"[ntfy] {name}: {len(mine)} cars")
                except Exception as exc:  # noqa: BLE001
                    log.error("ntfy to %s failed: %s", name, exc)
    for line in sent:
        log.info(line)
    return sent
