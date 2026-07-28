"""GitHub PR operations via the `gh` CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ExternalCommandError


@dataclass(frozen=True)
class PullRequest:
    """A PR's URL and number; `number` is None when gh didn't report one."""

    url: str
    number: int | None
    branch: str = ""


class Github:
    """Thin wrapper around the `gh` CLI for PR operations."""

    def __init__(
        self,
        repo_root: Path,
        runner: CommandRunner | None = None,
        *,
        binary: str = "gh",
    ) -> None:
        """Bind the repo root, a CommandRunner, and the gh binary name."""
        self.repo_root = repo_root
        self.runner = runner or SubprocessRunner()
        self.binary = binary

    def find_open_pr_for_branch(
        self, branch: str, *, base: str | None = None
    ) -> PullRequest | None:
        """Return the first open PR from `branch` (optionally into `base`), or None."""
        # `gh pr list` exits 0 with an empty array when no PRs match; treat
        # any other non-zero (auth, network) as fatal via the check=True
        # default — callers should not silently proceed if gh is broken.
        args = [
            self.binary,
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url,number,baseRefName",
        ]
        if base is not None:
            args.extend(["--base", base])
        result = self.runner.run(args, cwd=self.repo_root)
        raw = result.stdout.strip() or "[]"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError(
                f"gh pr list returned non-JSON output: {exc}\n{raw[:200]}"
            ) from exc
        if not isinstance(payload, list) or not payload:
            return None
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if base is not None and entry.get("baseRefName") != base:
                continue
            url = str(entry.get("url", ""))
            number = entry.get("number")
            return PullRequest(
                url=url, number=number if isinstance(number, int) else None
            )
        return None

    def find_open_prs_for_branch_prefix(
        self, prefix: str, *, base: str | None = None, limit: int = 200
    ) -> tuple[PullRequest, ...]:
        """Return open PRs whose head branch starts with `prefix`, lowest number first.

        Only the prefix is ours to predict: Renovate derives the rest of the
        branch name itself (group slug, slugification, a `major-` segment when
        majors are separated). Matching on the prefix keeps callers correct
        without re-deriving that algorithm. `gh pr list --head` is an exact
        match, so the filtering happens here.
        """
        args = [
            self.binary,
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "url,number,baseRefName,headRefName",
        ]
        if base is not None:
            args.extend(["--base", base])
        result = self.runner.run(args, cwd=self.repo_root)
        raw = result.stdout.strip() or "[]"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalCommandError(
                f"gh pr list returned non-JSON output: {exc}\n{raw[:200]}"
            ) from exc
        if not isinstance(payload, list):
            return ()
        found: list[PullRequest] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if base is not None and entry.get("baseRefName") != base:
                continue
            branch = str(entry.get("headRefName", ""))
            if not branch.startswith(prefix):
                continue
            number = entry.get("number")
            found.append(
                PullRequest(
                    url=str(entry.get("url", "")),
                    number=number if isinstance(number, int) else None,
                    branch=branch,
                )
            )
        return tuple(sorted(found, key=lambda pr: (pr.number is None, pr.number or 0, pr.branch)))

    def read_file_at_ref(self, path: str, ref: str) -> str:
        """Return a repository file's contents as of `ref`, via the GitHub API.

        Reading through the API rather than a local fetch keeps the caller's
        checkout untouched: upgrade branches live only on the remote, and this
        command must not switch branches or write to `.git`. `{owner}`/`{repo}`
        are placeholders `gh` fills from the repository context.
        """
        result = self.runner.run(
            [
                self.binary,
                "api",
                f"repos/{{owner}}/{{repo}}/contents/{path}?ref={ref}",
                "-H",
                "Accept: application/vnd.github.raw",
            ],
            cwd=self.repo_root,
        )
        return result.stdout

    def create_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PullRequest:
        """Create a PR via `gh pr create`.

        Returns PullRequest with number=None (gh doesn't print it); callers
        needing the number should re-query via `find_open_pr_for_branch`.
        """
        args = [
            self.binary,
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            head,
            "--base",
            base,
        ]
        if draft:
            args.append("--draft")
        result = self.runner.run(args, cwd=self.repo_root)
        # `gh pr create` prints the PR URL on stdout; warnings/notices can
        # precede it on some versions. Pick the last https:// line instead of
        # blindly trusting the final line of stdout.
        url = ""
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if candidate.startswith("https://") or candidate.startswith("http://"):
                url = candidate
        return PullRequest(url=url, number=None)
