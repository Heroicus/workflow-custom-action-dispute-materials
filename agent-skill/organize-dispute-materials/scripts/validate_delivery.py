#!/usr/bin/env python3
"""Validate one Base cell's native Feishu docx URL.

The URL host is deployment configuration, not a source-code constant.  Pass
one or more ``--allowed-host`` values from the environment's approved Feishu /
Lark tenant list.  The validator rejects whitespace, query strings, fragments,
userinfo, ports, non-HTTPS URLs, and non-docx paths.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class DeliveryValidationFailure(Exception):
    """A delivery URL cannot satisfy the Base field contract."""


def fail(message: str) -> None:
    """Write a stable diagnostic."""

    print(f"ERROR: {message}", file=sys.stderr)


def read_value(path: Path) -> str:
    """Read exactly one text value and preserve whitespace for validation."""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DeliveryValidationFailure(f"cannot read URL file: {exc}") from exc


def normalize_allowed_hosts(hosts: Iterable[str]) -> set[str]:
    """Normalize explicit host allow-list entries."""

    normalized = {host.strip().lower().rstrip(".") for host in hosts if host.strip()}
    if not normalized:
        raise DeliveryValidationFailure("at least one --allowed-host is required")
    if any("/" in host or ":" in host for host in normalized):
        raise DeliveryValidationFailure("allowed hosts must contain hostnames only")
    return normalized


def validate_url(raw: str, allowed_hosts: Iterable[str]) -> str:
    """Return the URL if it exactly matches the configured cloud-doc contract."""

    if not raw:
        raise DeliveryValidationFailure("URL value is empty")
    if raw != raw.strip():
        raise DeliveryValidationFailure("URL must not contain leading/trailing whitespace or a newline")
    if any(character.isspace() for character in raw):
        raise DeliveryValidationFailure("URL must not contain whitespace")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise DeliveryValidationFailure(f"URL cannot be parsed: {exc}") from exc
    hosts = normalize_allowed_hosts(allowed_hosts)
    if parsed.scheme != "https":
        raise DeliveryValidationFailure("URL scheme must be https")
    if not parsed.hostname:
        raise DeliveryValidationFailure("URL hostname is missing")
    host = parsed.hostname.lower().rstrip(".")
    if host not in hosts:
        raise DeliveryValidationFailure(f"URL host is not in the configured allow-list: {host}")
    if parsed.username or parsed.password:
        raise DeliveryValidationFailure("URL userinfo is forbidden")
    if parsed.port is not None:
        raise DeliveryValidationFailure("URL port is forbidden")
    if parsed.query or parsed.fragment:
        raise DeliveryValidationFailure("URL query and fragment are forbidden")
    pieces = parsed.path.split("/")
    if len(pieces) != 3 or pieces[1] != "docx" or not TOKEN_RE.fullmatch(pieces[2]):
        raise DeliveryValidationFailure("URL path must be exactly /docx/<token>")
    return raw


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(description="Validate a configured Feishu/Lark native docx URL.")
    parser.add_argument("url_file", type=Path, help="Text file containing exactly one URL")
    parser.add_argument(
        "--allowed-host",
        action="append",
        required=True,
        help="Approved Feishu/Lark hostname; repeat for multiple tenants",
    )
    parser.add_argument("--json", action="store_true", help="Print a JSON result")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point."""

    args = parse_args(argv)
    try:
        raw = read_value(args.url_file)
        value = validate_url(raw, args.allowed_host)
    except DeliveryValidationFailure as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            fail(str(exc))
        return 1
    result = {"ok": True, "url": value, "host": urlsplit(value).hostname}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
