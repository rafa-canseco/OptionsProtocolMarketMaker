#!/usr/bin/env python3
"""Path-only credential heuristic for exact Git index or working-tree content."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


_CREDENTIAL_NAME = r"""
    (?:[A-Z0-9]+[_-])*
    (?:
        PRIVATE[_-]?KEY
        | SECRET[_-]?KEY
        | API[_-]?KEY
        | ACCESS[_-]?KEY
        | SERVICE(?:[_-]?ROLE)?[_-]?KEY
        | SUPABASE(?:[_-]?(?:SERVICE[_-]?ROLE|ANON))?[_-]?KEY
        | AUTH[_-]?TOKEN
        | ACCESS[_-]?TOKEN
        | REFRESH[_-]?TOKEN
        | BEARER[_-]?TOKEN
        | [A-Z0-9]+(?:[_-][A-Z0-9]+)*[_-]TOKEN
        | CLIENT[_-]?SECRET
        | WEBHOOK[_-]?SECRET
        | PASSWORD
        | PASSWD
        | MNEMONIC
        | SEED(?:[_-]?PHRASE)?
    )
"""

_ASSIGNMENT_RE = re.compile(
    rf"""
    (?<![A-Z0-9_])
    [\"']?(?P<name>{_CREDENTIAL_NAME})[\"']?
    \s*(?::|=)\s*
    (?P<value>[\"'][^\n\"']*[\"']|[^\s,;\]\}}\#]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SEED_ASSIGNMENT_RE = re.compile(
    r"""
    (?<![A-Z0-9_])
    [\"']?(?P<name>(?:[A-Z0-9]+[_-])*(?:MNEMONIC|SEED(?:[_-]?PHRASE)?))[\"']?
    \s*(?::|=)\s*(?P<value>[^\n\#]+)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_CREDENTIALIZED_URL_RE = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:(?P<password>[^\s/@]+)@"
)
_BEARER_LITERAL_RE = re.compile(r"(?i)\bbearer\s+(?P<value>[a-z0-9._~+/=-]{16,})")
_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+(?:[ -][A-Z0-9]+)*[ -])?PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)
_SEED_WORD_RE = re.compile(r"[a-z]{3,}", re.IGNORECASE)

_PLACEHOLDER_WORDS = frozenset(
    {
        "changeme",
        "dummy",
        "empty",
        "example",
        "fake",
        "harness-offline",
        "local",
        "none",
        "not-a-secret",
        "placeholder",
        "redacted",
        "replace-me",
        "test",
        "todo",
    }
)
_PLACEHOLDER_PREFIXES = (
    "dummy-",
    "example-",
    "fake-",
    "harness-",
    "local-",
    "placeholder-",
    "redacted-",
    "test-",
    "your-",
)


def _strip_value(value: str) -> str:
    return value.strip().strip("\"'").strip()


def _is_placeholder(value: str) -> bool:
    normalized = _strip_value(value).lower()
    if len(normalized) < 8:
        return True
    if normalized in _PLACEHOLDER_WORDS or normalized.startswith(_PLACEHOLDER_PREFIXES):
        return True
    if "..." in normalized:
        return True
    if (
        normalized.startswith("<") and normalized.endswith(">")
    ) or normalized.startswith("${"):
        return True
    if normalized.startswith("{{"):
        return True
    if normalized.startswith("$"):
        return True
    return False


def _looks_like_seed(value: str) -> bool:
    normalized = _strip_value(value)
    return len(_SEED_WORD_RE.findall(normalized)) >= 12


def _assignment_is_sensitive(name: str, value: str) -> bool:
    normalized = _strip_value(value)
    if _is_placeholder(normalized):
        return False
    normalized_lower = normalized.lower()
    normalized_name = name.lower().replace("-", "_")
    if normalized_lower.startswith(("config.", "settings.", "self.", "os.getenv(")):
        return False
    if normalized_lower == normalized_name or normalized_lower.startswith(
        normalized_name + ")"
    ):
        return False
    upper_name = name.upper().replace("-", "_")
    if "MNEMONIC" in upper_name or "SEED" in upper_name:
        return _looks_like_seed(normalized) or len(normalized) >= 24
    if "PASSWORD" in upper_name or upper_name.endswith("PASSWD"):
        return len(normalized) >= 8
    return len(normalized) >= 16


def _line_is_sensitive(line: str) -> bool:
    if _PRIVATE_KEY_BLOCK_RE.search(line):
        return True

    for match in _SEED_ASSIGNMENT_RE.finditer(line):
        if _assignment_is_sensitive(match.group("name"), match.group("value")):
            return True

    for match in _ASSIGNMENT_RE.finditer(line):
        if _assignment_is_sensitive(match.group("name"), match.group("value")):
            return True

    for match in _CREDENTIALIZED_URL_RE.finditer(line):
        userinfo_value = match.group("password").rstrip("\"',;)}]")
        if not _is_placeholder(userinfo_value) and len(userinfo_value) >= 8:
            return True

    for match in _BEARER_LITERAL_RE.finditer(line):
        if not _is_placeholder(match.group("value")):
            return True

    return False


def _read_index(path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f":{path}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else None


def _read_working_tree(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("index", "working-tree"), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in sys.stdin.buffer.read().split(b"\0")
        if item
    ]
    read_content = _read_index if args.source == "index" else _read_working_tree
    failed_paths: list[str] = []

    for path in paths:
        content = read_content(path)
        if content is None or b"\0" in content:
            continue
        text = content.decode("utf-8", errors="ignore")
        if any(_line_is_sensitive(line) for line in text.splitlines()):
            failed_paths.append(path)

    for path in sorted(set(failed_paths)):
        print(
            f"sensitive-check: possible credential in {args.source} content: {path}",
            file=sys.stderr,
        )
    return 1 if failed_paths else 0


if __name__ == "__main__":
    raise SystemExit(main())
