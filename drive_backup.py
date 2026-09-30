#!/usr/bin/env python3
"""
drive_backup.py

Zips the harness source and uploads it to a folder in Google Drive.

Run it:
    python drive_backup.py            # from a terminal or from cron

What goes into the archive is the hard part, not the upload. This
directory holds live credentials (`.env`, `api_key.txt`, `token.json`,
`credentials.json`) and other people's data (the terms-acceptance
database, files correspondents sent, a log full of their mail). A backup
that swept any of that into Drive would be a leak with a nice filename.

So selection is an **allowlist first**: a file is archived only if it
matches one of `INCLUDE` — the Python modules, the tests, the deploy
files, the docs, the system prompt, the `.env.example` template and the
published terms. Anything new that appears in the directory is left out
until someone decides it belongs, which is the safe way round to be
wrong. The **denylist** `EXCLUDE` is then applied on top, and wins. It
should never have to fire for anything the allowlist admits; it is there
so that a too-generous pattern added to `INCLUDE` later cannot, by
itself, ship a secret.

The upload uses the same OAuth token and the same `drive.file` scope as
the rest of the harness. That scope only sees files this app created,
which is exactly what the folder lookup needs: the folder is one the app
made on its first run, and a same-named folder Steve made by hand is
invisible to it — a second one gets created rather than someone else's
folder being written into.
"""

from __future__ import annotations

import fnmatch
import logging
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

log = logging.getLogger("indi.backup")

ROOT = Path(__file__).resolve().parent

FOLDER_NAME = "Indi Harness Backups"
FOLDER_MIME = "application/vnd.google-apps.folder"
ARCHIVE_PREFIX = "indi-harness-"

# Paths are matched as POSIX paths relative to the root, with fnmatch.
# Note that fnmatch's `*` crosses `/`, so "tests/*.py" also admits
# tests/sub/x.py — intended for tests/ and deploy/, and the reason the
# top-level patterns below are written without a directory wildcard and
# checked against the depth separately (see `_is_top_level_pattern`).
INCLUDE: tuple[str, ...] = (
    "*.py",             # top-level modules only
    "*.md",             # top-level docs only
    "system_prompt.txt",
    ".env.example",
    ".gitignore",
    "tests/*.py",
    "deploy/*",
    "data/terms/*",
    "data/guidelines/*",
)

# Checked against every path component as well as the full path, so a
# `__pycache__` or `.git` anywhere in the tree excludes everything under it.
EXCLUDE: tuple[str, ...] = (
    # Secrets.
    ".env",
    ".env.*",           # except .env.example — see ALWAYS_OK_TEMPLATES
    "credentials.json",
    "token.json",
    "*token*.json",
    "api_key.txt",
    "*.pem",
    "*.key",
    # Tooling and caches.
    ".git",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    "indi.lock",
    # Logs, which quote correspondents' mail. Rotated ones too.
    "log.txt",
    "log.txt.*",
    "*.log",
    "*.log.*",
    # Other people's data.
    "data/attachments",
    "data/correspondents.db",
    "data/gmail_meta_cache.json",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*memory*.txt",
    "*memory*.json",
)

# The one `.env.*` file that is a template with no values in it.
ALWAYS_OK_TEMPLATES: frozenset[str] = frozenset({".env.example"})


def _is_top_level_pattern(pattern: str) -> bool:
    return "/" not in pattern


def is_included(rel: PurePosixPath) -> bool:
    """True if the relative path `rel` belongs in the archive."""
    text = rel.as_posix()
    allowed = False
    for pattern in INCLUDE:
        if _is_top_level_pattern(pattern):
            if len(rel.parts) == 1 and fnmatch.fnmatchcase(text, pattern):
                allowed = True
                break
        elif fnmatch.fnmatchcase(text, pattern):
            allowed = True
            break
    if not allowed:
        return False
    return not is_excluded(rel)


def is_excluded(rel: PurePosixPath) -> bool:
    """True if any denylist rule matches `rel`, one of its components, or
    one of its parent directories."""
    text = rel.as_posix()
    if text in ALWAYS_OK_TEMPLATES:
        return False
    parents = [p.as_posix() for p in rel.parents if p.as_posix() != "."]
    for pattern in EXCLUDE:
        if fnmatch.fnmatchcase(text, pattern):
            return True
        if any(fnmatch.fnmatchcase(part, pattern) for part in rel.parts):
            return True
        if any(fnmatch.fnmatchcase(parent, pattern) for parent in parents):
            return True
    return False


def select_files(root: Path) -> list[Path]:
    """Every file under `root` that passes `is_included`, sorted."""
    root = root.resolve()
    chosen = []
    for path in root.rglob("*"):
        # Symlinks are skipped outright: one pointing at ~/.ssh or at a
        # credential elsewhere would otherwise be archived under an
        # innocent name.
        if path.is_symlink() or not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if is_included(rel):
            chosen.append(path)
    return sorted(chosen)


def build_archive(root: Path = ROOT, out_dir: "Path | None" = None,
                  now: "datetime | None" = None) -> Path:
    """Zip the selected files under `root` into a fresh temp directory
    (or `out_dir`) and return the archive's path.

    The caller owns the file and should delete it once uploaded.
    """
    root = root.resolve()
    now = now or datetime.now(timezone.utc)
    if out_dir is None:
        out_dir = Path(tempfile.mkdtemp(prefix="indi-backup-"))
    archive = out_dir / f"{ARCHIVE_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}.zip"

    files = select_files(root)
    if not files:
        raise RuntimeError(f"Nothing to back up under {root}.")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, arcname=path.relative_to(root).as_posix())

    log.info("Built %s: %d file(s), %d bytes.",
             archive.name, len(files), archive.stat().st_size)
    return archive


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def ensure_folder(drive, name: str = FOLDER_NAME) -> str:
    """Return the id of the backup folder, creating it if the app cannot
    see one it made."""
    q = (f"name = '{_escape_drive_query(name)}' and "
         f"mimeType = '{FOLDER_MIME}' and trashed = false")
    found = (
        drive.files()
        .list(q=q, spaces="drive", fields="files(id, name)")
        .execute()
        .get("files", [])
    )
    if len(found) > 1:
        log.warning("Found %d folders named %r — using %s.",
                    len(found), name, found[0]["id"])
    if found:
        return found[0]["id"]
    created = (
        drive.files()
        .create(body={"name": name, "mimeType": FOLDER_MIME}, fields="id")
        .execute()
    )
    log.info("Created Drive folder %r (%s).", name, created["id"])
    return created["id"]


def upload_archive(drive, archive: Path, folder_name: str = FOLDER_NAME) -> tuple[str, str]:
    """Upload `archive` into the backup folder. Returns (file id, name)."""
    from googleapiclient.http import MediaFileUpload

    folder_id = ensure_folder(drive, folder_name)
    media = MediaFileUpload(str(archive), mimetype="application/zip", resumable=True)
    created = (
        drive.files()
        .create(body={"name": archive.name, "parents": [folder_id]},
                media_body=media, fields="id, name, size")
        .execute()
    )
    log.info("Uploaded %s to %r as %s (%s bytes).",
             created.get("name"), folder_name, created["id"], created.get("size"))
    return created["id"], created.get("name", archive.name)


def main() -> int:
    # Imported here so that the archive and upload functions above can be
    # used, and tested, without the Google and Anthropic client stack.
    from googleapiclient.discovery import build

    import check_and_reply
    from config import Config

    cfg = Config.from_env(non_interactive=True)
    check_and_reply.setup_logging(cfg.log_path, verbose=False)

    archive = None
    try:
        creds = check_and_reply.get_credentials(cfg)
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        archive = build_archive(cfg.base_dir)
        size = archive.stat().st_size
        file_id, name = upload_archive(drive, archive)
        log.info("Backup complete: %s (id %s, %d bytes).", name, file_id, size)
        return 0
    except SystemExit as exc:
        log.error("Backup failed: %s", exc)
        return 1
    except Exception:
        log.exception("Backup failed.")
        return 1
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
            try:
                archive.parent.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
