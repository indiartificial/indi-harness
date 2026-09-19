"""
config.py

All configuration in one place, as a frozen dataclass.

Two changes from the previous version worth calling out:

1. Every path is resolved relative to THIS FILE's directory, not the
   process working directory. The old version opened `.env`,
   `credentials.json`, `token.json` and `system_prompt.txt` relative to
   CWD, which meant running under cron (where CWD is usually $HOME)
   silently failed to find them — and, worse, a missing token.json sent
   `get_credentials()` into `flow.run_local_server()`, which tries to open
   a browser and blocks forever on a headless machine.

2. Config is passed around as an object instead of being read from module
   globals inside functions. That makes the pieces testable and makes it
   obvious at each call site what behaviour depends on what setting.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Explicit path — do not depend on the working directory.
load_dotenv(BASE_DIR / ".env")


# One .env can serve more than one machine: any setting may be given a
# platform-specific override by suffixing it, and the suffixed value wins
# where it applies.
#
#     INDI_ENABLE_LOCAL_EXEC=false
#     INDI_ENABLE_LOCAL_EXEC_LINUX=true
#
# The generic name stays the fallback, so a setting that's the same
# everywhere is still written once.
_PLATFORM_SUFFIX = "_WINDOWS" if sys.platform.startswith("win") else (
    "_MACOS" if sys.platform == "darwin" else "_LINUX"
)


def _env(name: str) -> "str | None":
    """Look up a setting, preferring this platform's override."""
    specific = os.environ.get(name + _PLATFORM_SUFFIX)
    if specific is not None and specific.strip() != "":
        return specific
    return os.environ.get(name)


def _bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = _env(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val.strip())
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got: {val!r}")


def _str(name: str, default: str) -> str:
    val = _env(name)
    return default if val is None else val.strip()


def _csv(name: str) -> tuple[str, ...]:
    """Comma-separated list env var -> tuple of lowercased, stripped items."""
    raw = _env(name) or ""
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def _load_api_key() -> str:
    """Load the Anthropic key from a dedicated file, not from .env.

    Keeping it out of .env means the config file can be read, copied,
    diffed, pasted into a chat or committed without carrying the
    credential with it. Precedence:

      1. The file at INDI_API_KEY_FILE (default: api_key.txt beside this
         script). First non-empty, non-comment line.
      2. The ANTHROPIC_API_KEY environment variable, for CI or a secrets
         manager that injects it.

    A key found in .env is refused outright rather than silently used, so
    the separation doesn't quietly erode over time.
    """
    key_file = _path("INDI_API_KEY_FILE", "api_key.txt")
    if key_file.exists():
        for line in key_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                _warn_if_world_readable(key_file)
                return line
        raise SystemExit(f"{key_file} exists but contains no key.")

    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        dotenv_path = BASE_DIR / ".env"
        if dotenv_path.exists() and "ANTHROPIC_API_KEY" in dotenv_path.read_text(
            encoding="utf-8", errors="replace"
        ):
            raise SystemExit(
                f"ANTHROPIC_API_KEY is set in {dotenv_path}. Move it to "
                f"{key_file} (one line, just the key) and delete the line from "
                ".env. If that key has ever been shared, rotate it first."
            )
        return env_key

    raise SystemExit(
        f"No API key found. Create {key_file} containing just the key on one "
        "line, or set ANTHROPIC_API_KEY in the environment."
    )


def _warn_if_world_readable(path: Path) -> None:
    """The key file is the one file here worth locking down."""
    if sys.platform.startswith("win"):
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        print(
            f"WARNING: {path} is readable by other users (mode {mode:o}). "
            f"Run: chmod 600 {path}",
            file=sys.stderr,
        )


def _optional_dir(name: str) -> "Path | None":
    """A directory setting that is allowed to be unset. Unlike _path, this
    never falls back to BASE_DIR — an unset root means the feature stays
    off rather than silently pointing at the script's own directory, which
    is where .env and the OAuth token live.

    `~` is expanded, so `~/indi-workspace` resolves correctly on Windows
    and on Linux from the same .env line."""
    val = _str(name, "")
    if not val:
        return None
    return Path(val).expanduser().resolve()


WILDCARD_PREFIXES = ("@", "*@")


def matches_sender(patterns: tuple[str, ...], addr: str) -> bool:
    """Match an address against a configured list.

    Supported entries:
      alice@example.com   exact address
      @example.com        any address at that domain
      *@example.com       same thing, alternative spelling
      *                   everyone
    """
    addr = (addr or "").strip().lower()
    if not addr or not patterns:
        return False
    for pattern in patterns:
        if pattern == "*":
            return True
        if pattern.startswith("*@"):
            pattern = pattern[1:]
        if pattern.startswith("@"):
            if addr.endswith(pattern):
                return True
        elif pattern == addr:
            return True
    return False


def has_wildcard(patterns: tuple[str, ...]) -> bool:
    return any(p == "*" or p.startswith(WILDCARD_PREFIXES) for p in patterns)


def _path(name: str, default: str) -> Path:
    """Resolve a configured path against BASE_DIR unless it's absolute."""
    val = _str(name, default)
    p = Path(val)
    return p if p.is_absolute() else (BASE_DIR / p)


@dataclass(frozen=True)
class Config:
    # --- credentials / paths ---
    api_key: str
    base_dir: Path
    credentials_path: Path
    token_path: Path
    system_prompt_path: Path
    lock_path: Path
    log_path: Path

    # --- model ---
    model: str
    max_tokens: int

    # --- sending behaviour ---
    send_automatically: bool
    max_unread_per_run: int

    # --- who we will talk to ---
    # allowed_senders: hard allowlist. Empty means "anyone may write in".
    # trusted_senders: senders whose messages may use the powerful tools
    #   (web fetch, code execution, cross-sender history search). Empty
    #   means nobody is trusted, which is the safe default — set this to
    #   your own address(es) in .env.
    allowed_senders: tuple[str, ...]
    trusted_senders: tuple[str, ...]
    # Senders whose history search may range over every correspondent
    # rather than only their own thread.
    history_all_senders: tuple[str, ...]
    max_replies_per_sender_per_day: int
    # Privileged tiers require the message to pass SPF/DKIM/DMARC. Without
    # this, granting powers by address grants them to anyone who can forge
    # that address — which is anyone.
    require_authenticated_sender: bool

    # --- tools ---
    enable_web_search: bool
    max_searches_per_reply: int
    enable_web_fetch: bool
    max_fetches_per_reply: int
    web_fetch_allowed_domains: tuple[str, ...]
    enable_code_execution: bool
    enable_history_search: bool
    max_history_search_results: int
    max_tool_loop_iterations: int

    # --- attachments ---
    max_incoming_attachment_mb: int
    upload_text_attachments: bool
    max_inline_attachment_chars: int
    max_outgoing_attachment_bytes: int

    # --- memory ---
    memory_batch_size: int
    memory_max_tokens: int
    memory_keep_backup: bool

    # --- storage ---
    max_body_chars: int

    # --- initiative (heartbeat runs) ---
    enable_outbound: bool
    outbound_max_per_day: int
    outbound_max_per_recipient_per_day: int
    outbound_allow_new_recipients: bool
    journal_max_per_day: int
    heartbeat_journal_entries: int

    # --- self-modification ---
    enable_self_code: bool
    self_code_senders: tuple[str, ...]
    self_code_auto_apply: bool
    self_code_test_command: str
    self_code_test_timeout: int
    max_self_code_changes_per_run: int
    self_code_min_size_ratio: float

    # --- local machine access (off by default; see local_tools.py) ---
    # These are the only settings in this file that can affect anything
    # outside this process. `local_tools_senders` is deliberately separate
    # from `trusted_senders`: being trusted enough for web fetch is not the
    # same as being trusted with a shell.
    local_tools_senders: tuple[str, ...]
    enable_local_files: bool
    local_files_root: Path | None
    local_files_writable_root: Path | None
    local_file_extensions: tuple[str, ...]
    max_local_file_bytes: int
    max_local_listing_entries: int
    enable_local_exec: bool
    local_exec_backend: str
    local_exec_image: str
    local_exec_timeout: int
    local_exec_memory_mb: int
    local_exec_allow_network: bool
    max_local_code_chars: int
    max_local_output_chars: int
    max_local_tool_calls_per_reply: int

    # --- runtime flags, set from the CLI rather than .env ---
    dry_run: bool = False
    non_interactive: bool = False

    # Tool version strings and their required beta headers. Kept here so
    # there is one place to bump when Anthropic ships a new tool version.
    web_search_tool_type: str = "web_search_20250305"
    web_fetch_tool_type: str = "web_fetch_20250910"
    web_fetch_beta: str = "web-fetch-2025-09-10"
    code_execution_tool_type: str = "code_execution_20250825"
    code_execution_beta: str = "code-execution-2025-08-25"
    files_api_beta: str = "files-api-2025-04-14"

    @classmethod
    def from_env(cls, dry_run: bool = False, non_interactive: bool = False) -> "Config":
        api_key = _load_api_key()

        return cls(
            api_key=api_key,
            base_dir=BASE_DIR,
            credentials_path=_path("INDI_CREDENTIALS_PATH", "credentials.json"),
            token_path=_path("INDI_TOKEN_PATH", "token.json"),
            system_prompt_path=_path("INDI_SYSTEM_PROMPT_PATH", "system_prompt.txt"),
            lock_path=_path("INDI_LOCK_PATH", "indi.lock"),
            log_path=_path("INDI_LOG_PATH", "log.txt"),
            model=_str("INDI_MODEL", "claude-sonnet-4-5"),
            max_tokens=_int("INDI_MAX_TOKENS", 2000),
            send_automatically=_bool("INDI_SEND_AUTOMATICALLY", False),
            max_unread_per_run=_int("INDI_MAX_UNREAD_PER_RUN", 10),
            allowed_senders=_csv("INDI_ALLOWED_SENDERS"),
            trusted_senders=_csv("INDI_TRUSTED_SENDERS"),
            history_all_senders=_csv("INDI_HISTORY_ALL_SENDERS"),
            max_replies_per_sender_per_day=_int("INDI_MAX_REPLIES_PER_SENDER_PER_DAY", 10),
            require_authenticated_sender=_bool("INDI_REQUIRE_AUTHENTICATED_SENDER", True),
            enable_web_search=_bool("INDI_ENABLE_WEB_SEARCH", True),
            max_searches_per_reply=_int("INDI_MAX_SEARCHES_PER_REPLY", 3),
            enable_web_fetch=_bool("INDI_ENABLE_WEB_FETCH", False),
            max_fetches_per_reply=_int("INDI_MAX_FETCHES_PER_REPLY", 3),
            web_fetch_allowed_domains=_csv("INDI_WEB_FETCH_ALLOWED_DOMAINS"),
            enable_code_execution=_bool("INDI_ENABLE_CODE_EXECUTION", False),
            enable_history_search=_bool("INDI_ENABLE_HISTORY_SEARCH", True),
            max_history_search_results=_int("INDI_MAX_HISTORY_SEARCH_RESULTS", 5),
            max_tool_loop_iterations=_int("INDI_MAX_TOOL_LOOP_ITERATIONS", 4),
            max_incoming_attachment_mb=_int("INDI_MAX_INCOMING_ATTACHMENT_MB", 5),
            upload_text_attachments=_bool("INDI_UPLOAD_TEXT_ATTACHMENTS", False),
            max_inline_attachment_chars=_int("INDI_MAX_INLINE_ATTACHMENT_CHARS", 40000),
            max_outgoing_attachment_bytes=_int("INDI_MAX_OUTGOING_ATTACHMENT_BYTES", 1_000_000),
            memory_batch_size=_int("INDI_MEMORY_BATCH_SIZE", 5),
            memory_max_tokens=_int("INDI_MEMORY_MAX_TOKENS", 4000),
            memory_keep_backup=_bool("INDI_MEMORY_KEEP_BACKUP", True),
            # Google Sheets rejects any cell over 50,000 characters. Staying
            # well under that means one long email can't abort the run.
            max_body_chars=_int("INDI_MAX_BODY_CHARS", 45000),
            enable_outbound=_bool("INDI_ENABLE_OUTBOUND", True),
            outbound_max_per_day=_int("INDI_OUTBOUND_MAX_PER_DAY", 5),
            outbound_max_per_recipient_per_day=_int(
                "INDI_OUTBOUND_MAX_PER_RECIPIENT_PER_DAY", 1),
            outbound_allow_new_recipients=_bool(
                "INDI_OUTBOUND_ALLOW_NEW_RECIPIENTS", False),
            journal_max_per_day=_int("INDI_JOURNAL_MAX_PER_DAY", 24),
            heartbeat_journal_entries=_int("INDI_HEARTBEAT_JOURNAL_ENTRIES", 10),
            enable_self_code=_bool("INDI_ENABLE_SELF_CODE", True),
            self_code_senders=_csv("INDI_SELF_CODE_SENDERS"),
            self_code_auto_apply=_bool("INDI_SELF_CODE_AUTO_APPLY", True),
            self_code_test_command=_str("INDI_SELF_CODE_TEST_COMMAND",
                                        "python3 -m pytest tests -q"),
            self_code_test_timeout=_int("INDI_SELF_CODE_TEST_TIMEOUT", 300),
            max_self_code_changes_per_run=_int("INDI_MAX_SELF_CODE_CHANGES_PER_RUN", 2),
            self_code_min_size_ratio=float(_str("INDI_SELF_CODE_MIN_SIZE_RATIO", "0.6")),
            local_tools_senders=_csv("INDI_LOCAL_TOOLS_SENDERS"),
            enable_local_files=_bool("INDI_ENABLE_LOCAL_FILES", False),
            local_files_root=_optional_dir("INDI_LOCAL_FILES_ROOT"),
            local_files_writable_root=_optional_dir("INDI_LOCAL_FILES_WRITABLE_ROOT"),
            local_file_extensions=_csv("INDI_LOCAL_FILE_EXTENSIONS"),
            max_local_file_bytes=_int("INDI_MAX_LOCAL_FILE_BYTES", 1_000_000),
            max_local_listing_entries=_int("INDI_MAX_LOCAL_LISTING_ENTRIES", 200),
            enable_local_exec=_bool("INDI_ENABLE_LOCAL_EXEC", False),
            local_exec_backend=_str("INDI_LOCAL_EXEC_BACKEND", "docker").lower(),
            local_exec_image=_str("INDI_LOCAL_EXEC_IMAGE", "python:3.11-slim"),
            local_exec_timeout=_int("INDI_LOCAL_EXEC_TIMEOUT", 30),
            local_exec_memory_mb=_int("INDI_LOCAL_EXEC_MEMORY_MB", 512),
            local_exec_allow_network=_bool("INDI_LOCAL_EXEC_ALLOW_NETWORK", False),
            max_local_code_chars=_int("INDI_MAX_LOCAL_CODE_CHARS", 20000),
            max_local_output_chars=_int("INDI_MAX_LOCAL_OUTPUT_CHARS", 8000),
            max_local_tool_calls_per_reply=_int("INDI_MAX_LOCAL_TOOL_CALLS_PER_REPLY", 8),
            dry_run=dry_run,
            non_interactive=non_interactive,
        )

    # --- validation ------------------------------------------------------

    def validate_local_tools(self) -> list[str]:
        """Refuses configurations that would defeat the point of the roots.

        Returned strings are fatal errors — the caller exits on any. The
        checks that matter most: a root that contains this script would
        expose .env, credentials.json and token.json, and a writable root
        equal to the read root lets a write turn into a later read of
        attacker-chosen content.
        """
        errors: list[str] = []
        if not (self.enable_local_files or self.enable_local_exec
                or self.enable_self_code):
            return errors

        if not (self.enable_local_files or self.enable_local_exec):
            return errors

        if not self.local_tools_senders:
            errors.append(
                "Local file/exec tools are enabled but INDI_LOCAL_TOOLS_SENDERS "
                "is empty. Set it to the addresses allowed to use them, or "
                "disable the tools."
            )
        elif has_wildcard(self.local_tools_senders):
            # This is the one place a wildcard is refused. Reading files on
            # the host and running code on the host are the two capabilities
            # where "anyone who emails this address" means an unauthenticated
            # stranger gets a shell. Every other tier accepts '*'.
            errors.append(
                "INDI_LOCAL_TOOLS_SENDERS contains a wildcard ('*' or a whole "
                "domain). Local file access and local code execution can only "
                "be granted to explicitly named addresses. List them "
                "individually, or disable INDI_ENABLE_LOCAL_FILES / "
                "INDI_ENABLE_LOCAL_EXEC."
            )
        if has_wildcard(self.trusted_senders) and (
            self.enable_local_files or self.enable_local_exec
        ):
            errors.append(
                "INDI_TRUSTED_SENDERS contains a wildcard while local tools are "
                "enabled. Trust is the second lock on local access; a wildcard "
                "there leaves only one. Name the trusted addresses explicitly."
            )

        if self.enable_local_files:
            if self.local_files_root is None:
                errors.append(
                    "INDI_ENABLE_LOCAL_FILES is on but INDI_LOCAL_FILES_ROOT is "
                    "not set. There is no default — point it at a specific "
                    "directory."
                )
            else:
                errors.extend(self._check_root(self.local_files_root, "INDI_LOCAL_FILES_ROOT"))

        writable = self.local_files_writable_root
        if writable is not None:
            errors.extend(self._check_root(writable, "INDI_LOCAL_FILES_WRITABLE_ROOT"))
            if self.local_files_root and writable == self.local_files_root:
                errors.append(
                    "INDI_LOCAL_FILES_WRITABLE_ROOT must not be the same "
                    "directory as INDI_LOCAL_FILES_ROOT — keep what can be "
                    "written separate from what gets read back."
                )

        if self.enable_self_code and has_wildcard(self.self_code_senders):
            errors.append(
                "INDI_SELF_CODE_SENDERS contains a wildcard. Anyone who can "
                "propose code changes can, over a few steps, reach the code "
                "that decides what anyone is allowed to do. Name the addresses "
                "individually."
            )

        if self.enable_local_exec:
            if self.local_exec_backend not in ("docker", "subprocess"):
                errors.append(
                    "INDI_LOCAL_EXEC_BACKEND must be 'docker' or 'subprocess'."
                )
            if self.local_exec_backend == "subprocess" and sys.platform.startswith("win"):
                errors.append(
                    "INDI_LOCAL_EXEC_BACKEND=subprocess is not supported on "
                    "Windows: the resource limits it depends on are POSIX-only, "
                    "so it would run unrestricted. Use the docker backend."
                )
            if self.local_exec_timeout < 1 or self.local_exec_timeout > 600:
                errors.append("INDI_LOCAL_EXEC_TIMEOUT must be between 1 and 600.")

        return errors

    def _check_root(self, root: Path, label: str) -> list[str]:
        errors: list[str] = []
        if not root.is_dir():
            errors.append(f"{label} is not an existing directory: {root}")
            return errors
        home = Path.home().resolve()
        if root == self.base_dir or self.base_dir == root or root in self.base_dir.parents:
            errors.append(
                f"{label} ({root}) contains this script's directory, which holds "
                ".env, credentials.json and token.json. Pick a narrower directory."
            )
        if root == Path("/") or root == home:
            errors.append(
                f"{label} ({root}) is your home or filesystem root. Pick a "
                "specific subdirectory."
            )
        for sensitive in (".ssh", ".aws", ".gnupg"):
            if (root / sensitive).exists():
                errors.append(
                    f"{label} ({root}) contains {sensitive}. Pick a narrower directory."
                )
        return errors

    # --- derived helpers -------------------------------------------------

    def may_use_local_tools(self, sender_addr: str) -> bool:
        """Two locks: the dedicated local-tools allowlist AND general trust."""
        return (
            matches_sender(self.local_tools_senders, sender_addr)
            and self.is_trusted(sender_addr)
        )

    def may_read_own_source(self, sender_addr: str) -> bool:
        """Reading its own code is harmless and explanatory; anyone in
        conversation with Indi can prompt it."""
        return self.enable_self_code

    def may_modify_own_source(self, sender_addr: str) -> bool:
        """Proposing code changes is gated on a named list. A wildcard here
        would mean any stranger could rewrite the harness, and the protected
        regions are the only thing standing between that and rewriting the
        guards themselves."""
        return (
            self.enable_self_code
            and bool(self.self_code_senders)
            and not has_wildcard(self.self_code_senders)
            and matches_sender(self.self_code_senders, sender_addr)
        )

    def may_search_all_history(self, sender_addr: str) -> bool:
        """Whether this sender's history search covers every correspondent
        or only their own thread."""
        return matches_sender(self.history_all_senders, sender_addr)

    def is_allowed(self, sender_addr: str) -> bool:
        if not self.allowed_senders:
            return True
        return matches_sender(self.allowed_senders, sender_addr)

    def is_trusted(self, sender_addr: str) -> bool:
        return matches_sender(self.trusted_senders, sender_addr)

    def betas(self, want_web_fetch: bool, want_code_execution: bool,
              want_files: bool) -> list[str]:
        """Beta headers required for the tools actually being sent.

        The previous version passed `web_fetch_20250910` and
        `code_execution_20250825` in `tools` but never sent any beta
        header, which the API requires for both. Uploading a file for a
        `container_upload` block additionally needs the Files API beta.
        """
        out: list[str] = []
        if want_web_fetch:
            out.append(self.web_fetch_beta)
        if want_code_execution:
            out.append(self.code_execution_beta)
        if want_files:
            out.append(self.files_api_beta)
        return out
