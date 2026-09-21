"""
terms.py

Loads the Terms of Service from disk.

The text lives in `data/terms/`, one file per version, named `vN.M.txt`.
Nothing about the terms is written in Python: a lawyer's edit should be a
new text file and nothing else, and the version string a correspondent
accepted should be derivable from the filename rather than from a
constant somebody forgot to bump.

The newest version — highest version number, not newest mtime, because a
restored backup must not silently demote the terms — is the current one.

Two loading failures are possible and they are treated very differently
by the gate in `terms_gate.py`:

  * The directory is missing or empty. `load()` returns None. The gate
    then has nothing to send a stranger, so it sends nothing at all and
    logs an error. It fails closed. A harness that cannot show someone
    the terms is not permitted to serve them instead.

  * A version file exists but is unreadable. Same answer, same reason.

The one thing this module will not do is fall back to some built-in
approximation of the terms. An approximation of a contract is not a
contract.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TERMS_DIR = BASE_DIR / "data" / "terms"

# v0.2.txt -> "0.2". Anything else in the directory is ignored rather
# than guessed at: a stray README or an editor's .swp file must not be
# mailed to anyone as if it were the agreement.
_VERSION_FILE = re.compile(r"^v(\d+(?:\.\d+)*)\.txt$")

log = logging.getLogger("indi.terms")


@dataclass(frozen=True)
class Terms:
    """One version of the agreement, as read from disk."""

    version: str      # "0.2"
    text: str         # the full agreement
    path: Path

    @property
    def filename(self) -> str:
        """What the attachment is called when it goes out."""
        return f"indi-terms-of-service-v{self.version}.txt"


def _sort_key(version: str) -> tuple[int, ...]:
    """Compare 0.10 as later than 0.2, which a string sort gets wrong."""
    return tuple(int(part) for part in version.split("."))


def available(directory: Path | None = None) -> list[tuple[str, Path]]:
    """Every version file present, oldest first."""
    directory = directory or TERMS_DIR
    found: list[tuple[str, Path]] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    for path in entries:
        match = _VERSION_FILE.match(path.name)
        if match and path.is_file():
            found.append((match.group(1), path))
    found.sort(key=lambda item: _sort_key(item[0]))
    return found


def load(directory: Path | None = None) -> Terms | None:
    """The current terms, or None if there are none to be had.

    None is a real answer here, not an error condition to be papered
    over — see the module docstring. Every caller has to decide what to
    do without terms, and the only safe decision is to say nothing.
    """
    directory = directory or TERMS_DIR
    versions = available(directory)
    if not versions:
        log.error(
            "No terms of service found in %s. Correspondents who have not "
            "already accepted cannot be served until a vN.M.txt file is "
            "put there.", directory,
        )
        return None

    version, path = versions[-1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("Could not read the terms at %s: %s", path, exc)
        return None

    if not text.strip():
        log.error("The terms file %s is empty.", path)
        return None

    return Terms(version=version, text=text, path=path)


def current_version(directory: Path | None = None) -> str:
    """The version string alone, or "" if the terms are unavailable."""
    terms = load(directory)
    return terms.version if terms else ""
