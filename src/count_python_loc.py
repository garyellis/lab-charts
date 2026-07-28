#!/usr/bin/env python3
"""Count physical lines of Python source code in a project."""

from __future__ import annotations

import argparse
import ast
import io
import tokenize
from pathlib import Path

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


def docstring_spans(source: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Return source spans occupied by real Python docstrings."""
    tree = ast.parse(source)
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue

        first_statement = node.body[0]
        if (
            isinstance(first_statement, ast.Expr)
            and isinstance(first_statement.value, ast.Constant)
            and isinstance(first_statement.value.value, str)
        ):
            value = first_statement.value
            spans.append(
                (
                    (value.lineno, value.col_offset),
                    (
                        value.end_lineno or value.lineno,
                        value.end_col_offset or value.col_offset,
                    ),
                )
            )

    return spans


def source_lines(path: Path, *, include_docstrings: bool) -> int:
    """Count non-blank, non-comment physical source lines in one file."""
    source = path.read_text(encoding="utf-8")
    excluded = [] if include_docstrings else docstring_spans(source)
    code_lines: set[int] = set()
    insignificant = {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in insignificant:
            continue
        if token.type == tokenize.STRING and any(
            start <= token.start and token.end <= end for start, end in excluded
        ):
            continue
        for line_number in range(token.start[0], token.end[0] + 1):
            code_lines.add(line_number)

    return len(code_lines)


def python_files(roots: list[Path]) -> list[Path]:
    """Find Python files below roots while skipping dependency/build directories."""
    files: set[Path] = set()

    for root in roots:
        if root.is_file():
            if root.suffix == ".py":
                files.add(root.resolve())
            continue

        for path in root.rglob("*.py"):
            if not any(part in IGNORED_DIRECTORIES for part in path.parts):
                files.add(path.resolve())

    return sorted(files)


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Count physical Python source lines, excluding blanks and comments.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[project_root],
        help=f"files or directories to scan (default: {project_root})",
    )
    parser.add_argument(
        "--include-docstrings",
        action="store_true",
        help="count docstrings as code, as tools such as cloc commonly do",
    )
    parser.add_argument(
        "--by-file",
        action="store_true",
        help="show a count for each file",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    files = python_files(args.paths)
    total = 0

    for path in files:
        try:
            count = source_lines(path, include_docstrings=args.include_docstrings)
        except (OSError, SyntaxError, tokenize.TokenError, UnicodeError) as error:
            print(f"warning: skipped {path}: {error}")
            continue

        total += count
        if args.by_file:
            print(f"{count:7}  {path}")

    print(f"{total:7}  Python source lines in {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
