from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


class Git:
    def __init__(self, root: Path, runner: CommandRunner | None = None) -> None:
        self.root = root
        self.runner = runner or CommandRunner()

    def is_repository(self) -> bool:
        result = self.runner.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.root, check=False
        )
        return result.returncode == 0

    @staticmethod
    def clone(
        url: str,
        target: Path,
        *,
        branch: str | None = None,
        depth: int | None = 1,
        runner: CommandRunner | None = None,
    ) -> None:
        runner = runner or CommandRunner()
        args = ["git", "clone"]
        if depth is not None:
            args.extend(["--depth", str(depth)])
        if branch is not None:
            args.extend(["--branch", branch])
        args.extend([url, str(target)])
        runner.run(args)

    def checkout_new_branch(self, branch: str, *, base: str | None = None) -> None:
        # `git checkout -B` creates-or-resets: callers re-running promote with
        # an aborted/leftover branch get a clean slate instead of an opaque
        # "branch already exists" failure mid-flow.
        args = ["git", "checkout", "-B", branch]
        if base is not None:
            args.append(base)
        self.runner.run(args, cwd=self.root)

    def add(self, paths: Sequence[Path | str]) -> None:
        if not paths:
            return
        self.runner.run(["git", "add", "--", *[str(p) for p in paths]], cwd=self.root)

    def commit(
        self, message: str, *, body: str | None = None, allow_empty: bool = False
    ) -> None:
        args = ["git", "commit", "-m", message]
        if body:
            args.extend(["-m", body])
        if allow_empty:
            args.append("--allow-empty")
        self.runner.run(args, cwd=self.root)

    def push(self, branch: str, *, remote: str = "origin", set_upstream: bool = True) -> None:
        args = ["git", "push"]
        if set_upstream:
            args.append("-u")
        args.extend([remote, branch])
        self.runner.run(args, cwd=self.root)

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        if not self.is_repository():
            raise ExternalCommandError(
                "not a git repository; changed chart detection requires git metadata"
            )
        result = self.runner.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=self.root
        )
        charts: set[str] = set()
        for line in result.stdout.splitlines():
            parts = Path(line).parts
            if len(parts) >= 2 and parts[0] == "charts":
                charts.add(parts[1])
        return sorted(charts)

    def changed_files(self, base: str = "origin/main") -> list[str]:
        """Return repo-relative paths changed vs `base`.

        Uses `...HEAD` (merge-base diff) so feature branches see only their
        own deltas, matching `changed_charts`. Uncommitted changes are NOT
        included — surface them by committing or by an explicit override at
        the CLI layer. Empty lines are filtered; output is sorted.
        """
        if not self.is_repository():
            raise ExternalCommandError(
                "not a git repository; changed file detection requires git metadata"
            )
        result = self.runner.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=self.root
        )
        files = {line for line in result.stdout.splitlines() if line.strip()}
        return sorted(files)
