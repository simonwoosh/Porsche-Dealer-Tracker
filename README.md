# Porsche Dealer Tracker

Tracks new and pre-owned inventory at the Porsche dealers in Washington, Oregon,
and Idaho. It records how long each car sits on the lot, notices when a car
sells, records its listed price at the time of sale, and alerts you by email
(or phone push).

| Dealer | Location |
|---|---|
| Porsche Bellevue | Bellevue, WA |
| Porsche Seattle North | Lynnwood, WA |
| Porsche Tacoma | Fife, WA |
| Porsche Spokane | Liberty Lake, WA |
| Porsche Beaverton | Beaverton, OR |
| Porsche Bend | Bend, OR |
| Porsche Boise | Boise, ID |

Add or pause dealers in [`dealers.yaml`](dealers.yaml). Every US Porsche
Center runs on the same Porsche website platform, so adding one (for example
in Montana) only takes its site URL.

## How it works

GitHub Actions runs the tracker every 4 hours. Each run:

1. Reads each dealer's new and pre-owned search pages (`/en/inventory/porsche/search`),
   following the site's robots.txt and pausing a few seconds between requests.
2. Saves every car by VIN in `data/inventory.db` (SQLite, committed back to the repo).
3. Compares with the previous run:
   - a VIN never seen before is an **arrival**, and its days on lot start counting
   - a price change is recorded in the car's price history
   - a car missing from **two complete scrapes in a row** is marked **sold**,
     with the last listed price as the price at time of sale
   - a car that reappears after being marked sold is put back on sale, and a car
     that shows up at another dealer is a transfer, not a sale
4. Emails subscribers about cars sold this run.
5. Publishes the dashboard to GitHub Pages.

A scrape only counts as complete when the number of cars found is at least 90%
of the count the dealer's site shows. Incomplete scrapes never mark cars sold,
so a site outage or layout change can't trigger a flood of false alerts.

**About days on lot:** cars already listed when tracking starts are shown as
"≥ N days", because their real arrival date isn't known. Accurate figures
build up as new cars arrive.

**About "sold":** the dealer site doesn't say why a car disappeared. Nearly
always it's a sale, but a car can also be pulled for a dealer trade or a
wholesale. The dashboard calls these sold and keeps the last seen date.

## Setup

1. **Secrets** (Settings → Secrets and variables → Actions → New repository secret):

   | Secret | Example |
   |---|---|
   | `SUBSCRIBERS_YAML` | contents of [`subscribers.example.yaml`](subscribers.example.yaml) with real addresses |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | `you@gmail.com` |
   | `SMTP_PASSWORD` | a Gmail [app password](https://myaccount.google.com/apppasswords) (not your normal password) |
   | `SMTP_FROM` | `Porsche Tracker <you@gmail.com>` (optional) |

   For phone push instead of (or as well as) email, install the
   [ntfy](https://ntfy.sh) app, subscribe to a hard-to-guess topic, and add
   `ntfy_topic:` to your subscriber entry. No SMTP needed for that.

2. **Dashboard:** Settings → Pages → Source: **GitHub Actions**. GitHub Pages
   needs the repo to be public, or a GitHub Pro account for a private repo.
   After the first deploy, add a repository *variable* `DASHBOARD_URL` with
   the Pages URL so alert emails link to it.

3. **Start it:** Actions → *Track inventory* → *Run workflow*. After that it
   runs on its own every 4 hours.

4. **Check alerts:** run `python -m tracker test-alert` locally with the same
   environment variables, or just wait for the first sale.

## Running locally

```bash
pip install -r requirements.txt
python -m tracker -v run --no-alerts --dealer bellevue   # one dealer, no alerts
python -m tracker site                                   # rebuild site/ from the database
python -m http.server -d site                            # open http://localhost:8000
```

`--save-html DIR` keeps page 1 of each search so a parsing problem can be
diagnosed. The scheduled workflow uploads these as the `debug-html` artifact.

## Tests

```bash
pip install pytest
python tests/fixtures/make_fixtures.py tests/fixtures
pytest -q
```

The tests cover VIN validation, robots.txt rules, parsing, the full arrival,
price change, sale, relist, and transfer lifecycle, subscriber filters, and an
end-to-end run against a fake dealer site.
