"""Generate synthetic dealer pages in two styles: rendered cards only, and RSC JSON only."""
import json, sys
from pathlib import Path

CARS = [
    ("WP0AD2Y14TSA36058", 2026, "Taycan GTS", 176705, "Black", "Black", None),
    ("WP1AG2A59TLB40863", 2026, "Macan S", 103945, "Chalk", "Red", None),
    ("WP0AA2YA8TL001541", 2026, "Panamera 4", 147305, "Ice Grey Metallic", "Black", None),
    ("WP1AG2A5XTLB42265", 2026, "Macan S", 101495, "Dolomite Silver Metallic", "Gray", None),
    ("WP1AL2AY1TDA15839", 2026, "Cayenne S", 137115, "White", "Black", None),
]

def card(c):
    vin, year, model, price, ext, inte, miles = c
    return f'''<li class="card"><a href="/en/inventory/porsche/details/{vin}">
<h3><span>{year}</span> <span>{model}</span></h3></a>
<div><span>Exterior: {ext}</span><span>Interior: {inte}</span></div>
<p>VIN <span>{vin}</span></p><p>Stock No. <span>{vin[-8:]}</span></p>
<p>Price (excl. taxes) <strong>${price:,}.00</strong></p>
<p>Est. ${price//60:,}/mo</p></li>'''

def cards_page(cars, totals="New(146) Pre-Owned(92) Certified Pre-Owned(56)"):
    return f"<html><body><aside>{totals}</aside><ul>{''.join(card(c) for c in cars)}</ul></body></html>"

def rsc_page(cars):
    items = [{"vehicle": {"vin": c[0], "modelYear": c[1], "modelName": c[2],
              "price": {"value": c[3], "currency": "USD"}, "exteriorColor": {"name": c[4]},
              "stockNumber": c[0][-8:], "mileage": {"value": 5, "unit": "mi"}}} for c in cars]
    flight = "0:[\"$\",\"div\",null,{}]\n1a:" + json.dumps({"results": items, "total": len(cars)}) + "\n"
    half = len(flight) // 2
    pushes = "".join(f"<script>self.__next_f.push([1,{json.dumps(part)}])</script>"
                     for part in (flight[:half], flight[half:]))
    return f"<html><body><div>New (5)</div>{pushes}</body></html>"

if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    (out / "cards.html").write_text(cards_page(CARS))
    (out / "rsc.html").write_text(rsc_page(CARS))
