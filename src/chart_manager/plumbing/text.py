"""Size caps for text that came off a subprocess or a log stream.

Lives in `plumbing/` because neither function knows anything about
HelmReleases, Flux, or diagnostics reports -- they are pure `str -> str`.
They previously sat in `services/helmrelease/_common.py`, which meant any
other service that wanted a bounded log blob either imported from a sibling
service package or grew its own copy.

Both append a marker rather than truncating silently: an operator reading a
capped report needs to know the tail was dropped, otherwise "the logs end
here" reads as "the process stopped here".
"""
from __future__ import annotations

__all__ = ["truncate_bytes", "truncate_lines"]


def truncate_lines(blob: str, max_lines: int) -> str:
    """Cap `blob` at `max_lines`, appending a truncated-line count marker."""
    if not blob:
        return ""
    lines = blob.splitlines()
    if len(lines) <= max_lines:
        return blob.rstrip("\n")
    head = lines[:max_lines]
    head.append(f"... ({len(lines) - max_lines} more line(s) truncated)")
    return "\n".join(head)


def truncate_bytes(blob: str, max_bytes: int) -> str:
    """Cap `blob` at `max_bytes` of UTF-8, appending a truncated-byte count marker."""
    if not blob:
        return ""
    encoded = blob.encode("utf-8")
    if len(encoded) <= max_bytes:
        return blob
    # Decode with errors="ignore" so we never split a multibyte codepoint
    # mid-sequence and emit an invalid utf-8 boundary.
    head = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return f"{head}\n... ({len(encoded) - max_bytes} more byte(s) truncated)"
