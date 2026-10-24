"""Run the real CLI against a fake dealer site served on localhost."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from make_fixtures import card  # noqa: E402

from tracker.__main__ import main  # noqa: E402

NEW = [
    ("WP0AD2Y14TSA36058", 2026, "Taycan GTS", 176705, "Black", "Black", None),
    ("WP1AG2A59TLB40863", 2026, "Macan S", 103945, "Chalk", "Red", None),
    ("WP0AA2YA8TL001541", 2026, "Panamera 4", 147305, "Ice Grey Metallic", "Black", None),
]
STATE = {"new": list(NEW), "used": [("WP0AA2A99TS207294", 2024, "911 Carrera", 129900, "GT Silver", "Black", None)]}
PER_PAGE = 2


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/robots.txt":
            body = "User-agent: *\nDisallow: /en/inventory/porsche/search?*sort=\n"
        else:
            q = parse_qs(u.query)
            cond = q["condition"][0]
            page = int(q.get("page", ["1"])[0])
            cars = STATE[cond]
            chunk = cars[(page - 1) * PER_PAGE: page * PER_PAGE] or cars[-PER_PAGE:]  # past the end: repeat last page
            totals = f"New ({len(STATE['new'])}) Pre-Owned ({len(STATE['used'])})"
            body = f"<html><body><aside>{totals}</aside><ul>{''.join(card(c) for c in chunk)}</ul></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())


def test_full_cycle(tmp_path, monkeypatch, capsys):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = {"dealers": [{"id": "fake", "name": "Porsche Fake", "city": "X", "state": "WA", "base_url": base}],
           "conditions": ["new", "used"],
           "scrape": {"delay_seconds": 0, "max_pages": 10, "missed_runs_before_sold": 2, "min_completeness": 0.9}}
    (tmp_path / "dealers.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("SUBSCRIBERS_YAML", "subscribers: [{name: T, email: t@example.com}]")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    clock = iter(f"2026-10-{d:02d}T12:00:00+00:00" for d in range(1, 30))
    monkeypatch.setattr("tracker.db.now_iso", lambda: next(clock))
    args = ["--config", str(tmp_path / "dealers.yaml"), "--db", str(tmp_path / "db.sqlite"),
            "--site-dir", str(tmp_path / "site"), "run", "--strict"]
    try:
        assert main(args) == 0
        out = capsys.readouterr().out
        assert "new: 3/3" in out and "used: 1/1" in out and "complete" in out
        STATE["new"] = NEW[1:]  # Taycan disappears
        main(args)
        main(args)
        out = capsys.readouterr().out
        assert "sold this run: 1" in out
        data = json.loads((tmp_path / "site" / "data.json").read_text())
        sold = [v for v in data["vehicles"] if v["status"] == "sold"]
        assert [v["vin"] for v in sold] == ["WP0AD2Y14TSA36058"]
        assert sold[0]["sale_price"] == 176705
        assert (tmp_path / "site" / "index.html").exists()
    finally:
        server.shutdown()
        STATE["new"] = list(NEW)
