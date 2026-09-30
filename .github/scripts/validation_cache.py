"""Stage disposable CI caches without replacing checkout-owned dependencies."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _tracked(root: Path) -> set[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return set(result.stdout.decode().split("\0"))


def _inputs(chart: Path) -> str:
    digest = hashlib.sha256()
    for name in ("Chart.yaml", "Chart.lock"):
        path = chart / name
        digest.update(name.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"missing")
    return digest.hexdigest()


def _safe_file(path: Path, root: Path) -> bool:
    return (
        path.is_file()
        and not path.is_symlink()
        and all(
            not parent.is_symlink()
            for parent in path.parents
            if parent != root and root in parent.parents
        )
    )


def dependencies(root: Path, cache: Path, *, restore: bool) -> None:
    """Copy untracked dependency files; matched inputs and checkout bytes win."""
    tracked = _tracked(root)
    if not restore:
        shutil.rmtree(cache, ignore_errors=True)
    for chart in sorted((root / "charts").glob("*")):
        if not chart.is_dir() or chart.is_symlink() or not (chart / "Chart.yaml").is_file():
            continue
        entry = cache / chart.name
        source = entry / "files" if restore else chart / "charts"
        if source.is_symlink() or entry.is_symlink():
            continue
        if restore:
            stamp = entry / "inputs.sha256"
            if not stamp.is_file() or stamp.read_text() != _inputs(chart):
                continue
        else:
            entry.mkdir(parents=True, exist_ok=True)
            (entry / "inputs.sha256").write_text(_inputs(chart))
        for path in sorted(source.rglob("*")):
            if not _safe_file(path, source):
                continue
            relative = path.relative_to(source)
            if ".git" in relative.parts:
                continue
            destination = chart / "charts" / relative if restore else entry / "files" / relative
            checkout_path = chart / "charts" / relative
            if checkout_path.relative_to(root).as_posix() in tracked:
                continue
            if restore and (
                destination.exists()
                or destination.is_symlink()
                or any(
                    parent.is_symlink() or (parent.exists() and not parent.is_dir())
                    for parent in destination.parents
                    if root in parent.parents
                )
            ):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def prune_derived(root: Path) -> None:
    """Save current chart results, excluding generations inherited from old runs."""
    manifest = root / "current-charts.json"
    current = json.loads(manifest.read_text()) if manifest.is_file() else []
    if not isinstance(current, list) or not all(
        isinstance(name, str)
        and len(name) == 69
        and name.endswith(".json")
        and all(char in "0123456789abcdef" for char in name[:-5])
        for name in current
    ):
        raise ValueError("invalid current derived-cache manifest")
    for path in (root / "charts").glob("*.json"):
        if path.name not in current:
            path.unlink()


if __name__ == "__main__":
    repository = Path.cwd()
    cache_root = repository / ".cache" / "chart-manager"
    command = sys.argv[1]
    if command == "prune-derived":
        prune_derived(cache_root / "schemas" / "v3" / "derived")
    elif command in {"restore-dependencies", "stage-dependencies"}:
        dependencies(
            repository, cache_root / "helm-dependencies", restore=command.startswith("restore")
        )
    else:
        raise SystemExit(f"unknown cache command: {command}")
