"""Fixed set of allowed medicine names + forgiving recognition of what the person said.

Speech-to-text often mangles drug names ("asprin", "vitamin dee"), so we match the
spoken name against the catalog: exact/alias first, then close spelling. Anything
that doesn't clearly match one catalog entry is rejected, never stored.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "medicine_catalog.json"

# Words people wrap around a name: "my aspirin pill", "the vitamin d tablets"
_FILLER = {
    "my", "the", "a", "an", "some", "please", "pill", "pills", "tablet", "tablets",
    "capsule", "capsules", "medicine", "medicines", "medication", "meds", "dose",
}
FUZZY_CUTOFF = 0.8
MAX_SPOKEN_NAMES = 30  # above this the "medicines I know" hint is not read out


def _normalize(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


@dataclass(frozen=True)
class MatchResult:
    name: str | None  # canonical catalog name, or None when not recognized
    suggestions: list[str] = field(default_factory=list)
    how: str = ""  # "exact" | "alias" | "fuzzy" | "ambiguous" | "unknown"


class MedicineCatalog:
    def __init__(self, entries: dict[str, list[str]]) -> None:
        if not entries:
            raise ValueError("Medicine catalog is empty")
        self.names = sorted(entries)
        self._lookup: dict[str, tuple[str, str]] = {}  # normalized -> (canonical, how)
        for name, aliases in entries.items():
            self._lookup[_normalize(name)] = (name, "exact")
            for alias in aliases:
                self._lookup.setdefault(_normalize(alias), (name, "alias"))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "MedicineCatalog":
        path = Path(path or os.environ.get("MEDICINE_BUDDY_CATALOG") or DEFAULT_CATALOG_PATH)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls({m["name"].strip(): list(m.get("aliases", [])) for m in data["medicines"]})

    def match(self, spoken: str) -> MatchResult:
        key = _normalize(spoken or "")
        if not key:
            return MatchResult(None, [], "unknown")
        if key in self._lookup:
            name, how = self._lookup[key]
            return MatchResult(name, [name], how)

        close = difflib.get_close_matches(key, self._lookup, n=5, cutoff=FUZZY_CUTOFF)
        candidates = list(dict.fromkeys(self._lookup[c][0] for c in close))
        if len(candidates) == 1:
            return MatchResult(candidates[0], candidates, "fuzzy")
        if len(candidates) > 1:
            return MatchResult(None, candidates, "ambiguous")
        # Nothing close: offer looser suggestions so Reachy can ask "did you mean...?"
        loose = difflib.get_close_matches(key, self._lookup, n=3, cutoff=0.5)
        return MatchResult(None, list(dict.fromkeys(self._lookup[c][0] for c in loose)), "unknown")

    def find_in_text(self, sentence: str) -> set[str]:
        """Catalog medicines mentioned anywhere in a sentence ('add my asprin at nine' -> {'Aspirin'})."""
        words = re.sub(r"[^a-z0-9 ]+", " ", (sentence or "").lower()).split()
        found: set[str] = set()
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                chunk = " ".join(words[i : i + size])
                key = _normalize(chunk)
                if not key or len(key) < 3:
                    continue
                if key in self._lookup:
                    found.add(self._lookup[key][0])
                    continue
                close = difflib.get_close_matches(key, self._lookup, n=2, cutoff=0.85)
                names = {self._lookup[c][0] for c in close}
                if len(names) == 1:
                    found |= names
        return found

    def not_recognized_hint(self, spoken: str, result: MatchResult) -> str:
        if result.how == "ambiguous":
            return f"I'm not sure which medicine you mean by '{spoken}'. Did you mean {_or_list(result.suggestions)}?"
        if result.suggestions:
            return f"'{spoken}' is not on the medicine list. Did you mean {_or_list(result.suggestions)}?"
        if len(self.names) > MAX_SPOKEN_NAMES:  # big (Swiss) catalog: never read hundreds of names aloud
            return (f"'{spoken}' is not on the medicine list. Please say the name printed on the box, "
                    f"or ask a family member to add it.")
        return (f"'{spoken}' is not on the medicine list. "
                f"The medicines I know are: {', '.join(self.names)}.")


def _or_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]
