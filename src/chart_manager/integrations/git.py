"""git wrapper: clone, branch, add/commit/push, and changed-file detection."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, CheckStatus, probe_binary
from chart_manager.settings import DEFAULT_CHARTS_DIR, RepositoryLayout


class Git:
    """Run git subcommands rooted at one working tree."""

    def __init__(
        self,
        root: Path,
        runner: CommandRunner | None = None,
        *,
        charts_dir: Path = DEFAULT_CHARTS_DIR,
    ) -> None:
        """Bind the working-tree root and a CommandRunner."""
        self.root = root
        self.runner = runner or SubprocessRunner()
        self.layout = RepositoryLayout(root=root, charts_dir=charts_dir)

    def preflight(self) -> tuple[Check, ...]:
        """Report the git binary and whether `root` is actually a work tree.

        The second check is why this is not a bare binary probe: every
        changed-file selector in the CI verbs answers "nothing changed" for
        a directory that is not a checkout, which is indistinguishable from
        a clean tree and is the failure a preflight should name.
        """
        binary = probe_binary(
            self.runner,
            "git",
            name="git",
            remediation="install git -- https://git-scm.com/downloads",
        )
        if binary.status is not CheckStatus.OK:
            return (binary, Check.skipped("git-repository", "git unavailable"))
        if not self.is_repository():
            return (
                binary,
                Check.failed(
                    "git-repository",
                    f"{self.root} is not inside a git work tree",
                    remediation="run from a checkout, or point --root at one",
                    outcome=Outcome.ENVIRONMENT,
                ),
            )
        return (binary, Check.ok("git-repository", str(self.root)))

    def is_repository(self) -> bool:
        """True if `root` is inside a git work tree."""
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
        """Clone `url` into `target`; shallow (depth=1) by default."""
        runner = runner or SubprocessRunner()
        args = ["git", "clone"]
        if depth is not None:
            args.extend(["--depth", str(depth)])
        if branch is not None:
            args.extend(["--branch", branch])
        args.extend([url, str(target)])
        runner.run(args)

    def checkout_new_branch(self, branch: str, *, base: str | None = None) -> None:
        """Create-or-reset `branch` (optionally from `base`) and switch to it."""
        # `git checkout -B` creates-or-resets: callers re-running promote with
        # an aborted/leftover branch get a clean slate instead of an opaque
        # "branch already exists" failure mid-flow.
        args = ["git", "checkout", "-B", branch]
        if base is not None:
            args.append(base)
        self.runner.run(args, cwd=self.root)

    def add(self, paths: Sequence[Path | str]) -> None:
        """Stage the given paths; no-op on an empty list."""
        if not paths:
            return
        self.runner.run(["git", "add", "--", *[str(p) for p in paths]], cwd=self.root)

    def commit(
        self, message: str, *, body: str | None = None, allow_empty: bool = False
    ) -> None:
        """Commit staged changes; `body` becomes a second -m paragraph."""
        args = ["git", "commit", "-m", message]
        if body:
            args.extend(["-m", body])
        if allow_empty:
            args.append("--allow-empty")
        self.runner.run(args, cwd=self.root)

    def push(self, branch: str, *, remote: str = "origin", set_upstream: bool = True) -> None:
        """Push `branch` to `remote`, setting upstream by default."""
        args = ["git", "push"]
        if set_upstream:
            args.append("-u")
        args.extend([remote, branch])
        self.runner.run(args, cwd=self.root)

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        """Return chart names with committed changes vs `base` (merge-base diff).

        A "chart" is the first directory under the configured chart root.
        """
        if not self.is_repository():
            raise ExternalCommandError(
                "not a git repository; changed chart detection requires git metadata"
            )
        result = self.runner.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=self.root
        )
        charts: set[str] = set()
        for line in result.stdout.splitlines():
            chart = self.layout.chart_name_from_repo_path(line)
            if chart is not None:
                charts.add(chart)
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
