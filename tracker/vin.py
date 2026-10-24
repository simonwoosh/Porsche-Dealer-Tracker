"""VIN detection and North American check-digit validation."""
import re

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def is_valid_vin(vin: str) -> bool:
    """True if `vin` is a 17-char VIN with a valid check digit (position 9).

    Every vehicle sold new in North America since 1981 carries a check digit,
    which lets us reject random 17-character tokens (hashes, ids) in page source.
    """
    if not isinstance(vin, str) or len(vin) != 17 or not VIN_RE.fullmatch(vin):
        return False
    if not any(c.isdigit() for c in vin) or not any(c.isalpha() for c in vin):
        return False
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS))
    check = total % 11
    expected = "X" if check == 10 else str(check)
    return vin[8] == expected


def find_vins(text: str) -> list[str]:
    """All valid VINs in `text`, in order of first appearance, deduplicated."""
    seen: dict[str, None] = {}
    for m in VIN_RE.finditer(text):
        v = m.group(0)
        if v not in seen and is_valid_vin(v):
            seen[v] = None
    return list(seen)
