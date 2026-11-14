"""One canonical form of a person's name.

Speech-to-text returns "Nicolò", "nicolo" or "NICOLÒ" for the same person, and an é can be one
character or two (NFC / NFD). All of those must be the SAME person, never two: otherwise two
registrations that look identical exist side by side and buttons / lookups act on the wrong one.

name_key("Nicolò") == name_key("NICOLÒ") == name_key("nicolo") == name_key("  Nicòlo ")
"""

from __future__ import annotations

import re
import unicodedata

# Words people put in front of a name ("grandpa Mario", "nonna Rosa", "my uncle Luca").
# They are removed only when no registered person has that exact full name.
TITLES = frozenset("""
my our the dear mr mrs ms miss mister sir madam madame monsieur dr doctor
uncle aunt auntie grandpa grandma grandad granddad grandfather grandmother gramps granny nana nan
papa mama mum mom dad daddy papi mommy
nonno nonna zio zia signor signora signore
opa oma opi omi
grandpere grandmere papy mamie mamy
abuelo abuela tio tia
""".split())


def name_key(name: str) -> str:
    """Case-, accent-, spacing- and punctuation-insensitive key ('' when there is no letter or digit)."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = no_marks.casefold()
    return " ".join(re.sub(r"[^0-9a-zß-￿]+", " ", folded).split())


def strip_titles(key: str) -> str:
    """Remove leading title / kinship words from an already-normalised key."""
    words = key.split()
    while words and words[0] in TITLES:
        words.pop(0)
    return " ".join(words)
