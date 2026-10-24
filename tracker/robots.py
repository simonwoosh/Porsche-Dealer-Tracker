"""A small robots.txt checker that understands `*` and `$` wildcards.

Python's urllib.robotparser ignores wildcards, and Porsche dealer sites use them
heavily to block filter parameters, so we implement Google's matching rules:
the longest matching rule wins, and Allow wins a tie.
"""
import re
from urllib.parse import urlsplit


def _rule_regex(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    pattern = "".join(".*" if ch == "*" else re.escape(ch) for ch in path)
    return re.compile("^" + pattern + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str, user_agent: str = "*"):
        self.rules: list[tuple[bool, int, re.Pattern]] = []
        groups = self._parse(text)
        ua = user_agent.lower()
        chosen = None
        for agents, rules in groups:
            if any(a != "*" and a in ua for a in agents):
                chosen = rules
                break
        if chosen is None:
            for agents, rules in groups:
                if "*" in agents:
                    chosen = rules
                    break
        for allow, path in chosen or []:
            if path:
                self.rules.append((allow, len(path), _rule_regex(path)))

    @staticmethod
    def _parse(text: str):
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                rules.append((key == "allow", value))
                last_was_agent = False
            else:
                last_was_agent = False
        if agents:
            groups.append((agents, rules))
        return groups

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        best: tuple[int, bool] | None = None
        for allow, length, rx in self.rules:
            if rx.match(target):
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]
