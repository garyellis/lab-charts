"""git wrapper: clone, branch, add/commit/push, and working-tree and revision queries."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from chart_manager.plumbing.commands import CommandResult, CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import Check, CheckStatus, probe_binary


class Git:
    """Run git subcommands rooted at one working tree, each capped at `timeout` seconds.

    `timeout=None` leaves each call unbounded.
    """

    def __init__(self, root: Path, runner: CommandRunner, *, timeout: float | None) -> None:
        """Bind the working-tree root, a CommandRunner and the per-call cap."""
        self.root = root
        self.runner = runner
        self.timeout = timeout

    def _run(self, args: list[str], *, check: bool = True) -> CommandResult:
        """Every git invocation in the working tree: `git <args>` within the cap."""
        return self.runner.run(["git", *args], cwd=self.root, check=check, timeout=self.timeout)

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
                    remediation="run from a checkout, or set CHART_MANAGER_ROOT",
                    outcome=Outcome.ENVIRONMENT,
                ),
            )
        return (binary, Check.ok("git-repository", str(self.root)))

    def is_repository(self) -> bool:
        """True if `root` is inside a git work tree."""
        return self._run(["rev-parse", "--show-toplevel"], check=False).returncode == 0

    @classmethod
    def clone(
        cls, url: str, root: Path, *, branch: str, runner: CommandRunner, timeout: float | None
    ) -> Git:
        """Shallow-clone `branch` of `url` into `root` and return the `Git` bound to it."""
        root.mkdir(parents=True, exist_ok=True)
        git = cls(root, runner, timeout=timeout)
        git._run(["clone", "--depth", "1", "--branch", branch, url, "."])
        return git

    def checkout_new_branch(self, branch: str, *, base: str) -> None:
        """Create-or-reset `branch` from `base` and switch to it."""
        # `git checkout -B` creates-or-resets: callers re-running promote with
        # an aborted/leftover branch get a clean slate instead of an opaque
        # "branch already exists" failure mid-flow.
        self._run(["checkout", "-B", branch, base])

    def add(self, paths: Sequence[Path | str]) -> None:
        """Stage the given paths; no-op on an empty list."""
        if not paths:
            return
        self._run(["add", "--", *[str(p) for p in paths]])

    def commit(self, message: str, *, body: str) -> None:
        """Commit staged changes; `body` becomes a second -m paragraph."""
        self._run(["commit", "-m", message, "-m", body])

    def push(self, branch: str, *, remote: str = "origin", set_upstream: bool = True) -> None:
        """Push `branch` to `remote`, setting upstream by default."""
        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.extend([remote, branch])
        self._run(args)

    def status_paths(self, paths: Sequence[Path]) -> tuple[str, ...]:
        """Return the modified or untracked files under `paths` (relative to `root`)."""
        result = self._run(
            ["status", "--porcelain=v1", "--untracked-files=all", "--", *map(str, paths)]
        )
        return tuple(
            line[3:].strip()
            for line in result.stdout.splitlines()
            if len(line) > 3 and line[3:].strip()
        )

    def remote_url(self) -> str | None:
        """Return the `origin` remote's URL, or None when there is no such remote."""
        result = self._run(["remote", "get-url", "origin"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def show(self, revision: str, path: Path) -> str:
        """Return `path` (relative to `root`) as it was at `revision`."""
        return self._run(["show", f"{revision}:{path.as_posix()}"]).stdout

    def changed_files(self, base: str = "origin/main") -> list[str]:
        """Return paths changed vs `base`, relative to `root`.

        Uses `...HEAD` (merge-base diff) so feature branches see only their
        own deltas. Uncommitted changes are NOT included — surface them by
        committing or by an explicit override at the CLI layer. Empty lines
        are filtered; output is sorted. `--relative` drops changes outside
        `root` when it is a subdirectory of the checkout.
        """
        if not self.is_repository():
            raise ExternalCommandError(
                "not a git repository; changed file detection requires git metadata"
            )
        result = self._run(["diff", "--name-only", "--relative", f"{base}...HEAD"])
        files = {line for line in result.stdout.splitlines() if line.strip()}
        return sorted(files)
