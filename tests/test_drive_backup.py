"""
tests/test_drive_backup.py

The backup's whole risk is what ends up inside the zip, so most of this
file builds a fake harness tree seeded with fake secrets and fake
correspondent data and checks that none of it survives into the archive.
The rest checks the Drive folder lookup and upload against a fake
service, since the real one cannot be reached from a test run.
"""

import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import drive_backup  # noqa: E402

SECRET = "SECRET-VALUE-MUST-NOT-LEAK"

FORBIDDEN = [
    ".env",
    ".env.production",
    "credentials.json",
    "token.json",
    "refresh_token_backup.json",
    "api_key.txt",
    "server.pem",
    "private.key",
    "log.txt",
    "log.txt.1",
    "indi.lock",
    "data/attachments/alice@example.com/contract.pdf",
    "data/correspondents.db",
    "data/memory_alice.txt",
    "__pycache__/config.cpython-314.pyc",
    ".pytest_cache/v/cache/nodeids",
    ".git/config",
    "tests/__pycache__/test_x.pyc",
    "notes.txt",              # not on the allowlist at all
    "deploy/.env",            # allowlisted directory, denied name
]

EXPECTED = [
    "check_and_reply.py",
    "config.py",
    "README.md",
    "system_prompt.txt",
    ".env.example",
    "tests/test_something.py",
    "deploy/indi.service",
    "data/terms/v0.3.txt",
]


def _tree(root: Path) -> None:
    for rel in FORBIDDEN:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SECRET)
    for rel in EXPECTED:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("ok\n")


def _names(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as zf:
        return set(zf.namelist())


def test_archive_excludes_secrets_and_personal_data(tmp_path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    _tree(src)

    archive = drive_backup.build_archive(src, out_dir=out)
    names = _names(archive)

    for rel in FORBIDDEN:
        assert rel not in names, rel
    for name in names:
        base = PurePosixPath(name).name
        assert base not in {".env", "credentials.json", "token.json",
                            "api_key.txt", "log.txt"}, name
        assert not name.startswith("data/attachments/"), name
    for rel in EXPECTED:
        assert rel in names, rel

    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            assert SECRET not in zf.read(name).decode("utf-8", "replace"), name


def test_archive_name_is_timestamped(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n")
    from datetime import datetime, timezone
    when = datetime(2026, 9, 23, 17, 4, 5, tzinfo=timezone.utc)
    archive = drive_backup.build_archive(src, out_dir=tmp_path, now=when)
    assert archive.name == "indi-harness-20260923-170405.zip"


def test_symlinks_are_not_followed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text(SECRET)
    (src / "real.py").write_text("x = 1\n")
    (src / "innocent.py").symlink_to(outside)
    names = _names(drive_backup.build_archive(src, out_dir=tmp_path))
    assert names == {"real.py"}


def test_denylist_wins_over_allowlist():
    assert drive_backup.is_included(PurePosixPath("config.py"))
    assert drive_backup.is_included(PurePosixPath(".env.example"))
    assert not drive_backup.is_included(PurePosixPath("deploy/token.json"))
    assert not drive_backup.is_included(PurePosixPath("deploy/.env.local"))
    assert not drive_backup.is_included(PurePosixPath("tests/__pycache__/x.py"))
    # Top-level patterns do not reach into other directories.
    assert not drive_backup.is_included(PurePosixPath("data/attachments/x.py"))


def test_real_tree_selection_has_no_secrets():
    """The live directory, not a fake one: none of the files that really
    hold credentials or personal data here may be selected."""
    chosen = {p.relative_to(ROOT).as_posix()
              for p in drive_backup.select_files(ROOT)}
    for rel in (".env", "credentials.json", "token.json", "api_key.txt",
                "log.txt", "data/correspondents.db", "indi.lock"):
        assert rel not in chosen, rel
    assert not any(r.startswith((".git/", "data/attachments/")) for r in chosen)
    assert "drive_backup.py" in chosen


# ---------------------------------------------------------------------------
# Fake Drive
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Files:
    def __init__(self, existing):
        self.existing = existing
        self.list_calls = []
        self.created = []

    def list(self, **kw):
        self.list_calls.append(kw)
        return _Req({"files": list(self.existing)})

    def create(self, body, fields, media_body=None):
        self.created.append({"body": body, "media_body": media_body})
        if body.get("mimeType") == drive_backup.FOLDER_MIME:
            return _Req({"id": "folder-new"})
        return _Req({"id": "file-1", "name": body["name"], "size": "123"})


class FakeDrive:
    def __init__(self, existing=()):
        self._files = _Files(existing)

    def files(self):
        return self._files


def test_ensure_folder_reuses_existing():
    drive = FakeDrive([{"id": "folder-old", "name": drive_backup.FOLDER_NAME}])
    assert drive_backup.ensure_folder(drive) == "folder-old"
    assert drive._files.created == []
    q = drive._files.list_calls[0]["q"]
    assert drive_backup.FOLDER_NAME in q and drive_backup.FOLDER_MIME in q
    assert "trashed = false" in q


def test_ensure_folder_creates_when_missing():
    drive = FakeDrive()
    assert drive_backup.ensure_folder(drive) == "folder-new"
    assert drive._files.created[0]["body"] == {
        "name": drive_backup.FOLDER_NAME, "mimeType": drive_backup.FOLDER_MIME}


def test_upload_goes_into_folder(tmp_path):
    archive = tmp_path / "indi-harness-20260923-170405.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.py", "x = 1\n")
    drive = FakeDrive([{"id": "folder-old", "name": drive_backup.FOLDER_NAME}])

    file_id, name = drive_backup.upload_archive(drive, archive)

    assert (file_id, name) == ("file-1", archive.name)
    upload = drive._files.created[-1]
    assert upload["body"] == {"name": archive.name, "parents": ["folder-old"]}
    assert upload["media_body"] is not None
