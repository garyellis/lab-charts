"""Pinned Git checkouts used as local kubeconform repositories."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class RepositorySnapshotDirectoryNotFoundError(ExternalCommandError):
    """The pinned repository has no requested schema directory."""


class RepositorySnapshot:
    """Fetch once; inspect locally with lazy fetching and hooks disabled."""

    def __init__(self, runner: CommandRunner, *, timeout: float | None) -> None:
        self.runner = runner
        self.timeout = timeout

    def _git(self, root: Path, *args: str, online: bool = False) -> str:
        return self.runner.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=root,
            timeout=self.timeout,
            env={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "0" if online else "1",
            },
        ).stdout.strip()

    def checkout(
        self, repository: str, revision: str, destination: Path, *, directory: str | None = None
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
            raise ValueError("snapshot repository must be an owner/name GitHub repository")
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("snapshot revision must be a full commit SHA")
        if directory is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", directory):
            raise ValueError("snapshot directory must be one safe path component")
        destination.mkdir(parents=True, exist_ok=True)
        self._git(destination, "init", "--quiet")
        # Fetch one commit, without history. Sparse checkout avoids materializing
        # every historical Kubernetes version; the selected version is complete.
        self._git(destination, "remote", "add", "origin", f"https://github.com/{repository}.git")
        self._git(destination, "config", "remote.origin.promisor", "true")
        self._git(destination, "config", "remote.origin.partialclonefilter", "blob:none")
        self._git(
            destination,
            "fetch",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "origin",
            revision,
            online=True,
        )
        if directory is not None:
            if not self._git(
                destination, "ls-tree", "-d", "--name-only", "FETCH_HEAD", "--", directory
            ):
                raise RepositorySnapshotDirectoryNotFoundError(
                    f"{repository}@{revision} has no schema directory {directory}"
                )
            self._git(destination, "sparse-checkout", "set", "--cone", directory)
        self._git(destination, "checkout", "--quiet", "--detach", "FETCH_HEAD", online=True)
        problem = self.inspect(destination, revision, directory=directory)
        if problem:
            raise ExternalCommandError(f"invalid repository snapshot: {problem}")

    def inspect(self, root: Path, revision: str, *, directory: str | None = None) -> str | None:
        """Verify the pinned checkout and detect missing/modified tracked files."""
        if not (root / ".git").is_dir():
            return "Git snapshot is missing"
        try:
            if self._git(root, "rev-parse", "HEAD") != revision:
                return "snapshot HEAD differs from the pinned commit"
            tree = self._git(
                root, "ls-tree", "-rz", "--full-tree", revision, "--", directory or "."
            )
            expected: set[str] = set()
            resolved_root = root.resolve()
            checked_parents: set[Path] = set()
            for record in tree.split("\0"):
                if not record:
                    continue
                header, name = record.split("\t", 1)
                mode, kind, oid = header.split()
                if not name.endswith(".json"):
                    continue
                expected.add(name)
                path = root / name
                if mode not in {"100644", "100755"} or kind != "blob":
                    return f"schema is not a regular Git blob: {name}"
                if path.is_symlink():
                    return f"schema path escapes the snapshot: {name}"
                if path.parent not in checked_parents:
                    if not path.parent.resolve().is_relative_to(resolved_root):
                        return f"schema path escapes the snapshot: {name}"
                    checked_parents.add(path.parent)
                if not path.is_file():
                    return f"schema is missing: {name}"
                content = path.read_bytes()
                actual = hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False
                ).hexdigest()
                if actual != oid:
                    return f"schema differs from pinned Git blob: {name}"
            if not expected:
                return "snapshot has no schemas for the requested Kubernetes version/catalog"
            content_root = root / directory if directory else root
            actual_paths = {p.relative_to(root).as_posix() for p in content_root.rglob("*.json")}
            if actual_paths != expected:
                return "snapshot contains unexpected schema files"
        except (ExternalCommandError, OSError, ValueError) as exc:
            return f"cannot inspect Git snapshot: {exc}"
        return None
