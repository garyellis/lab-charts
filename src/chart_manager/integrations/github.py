from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int | None


class Github:
    """Thin wrapper around the `gh` CLI for PR operations."""

    def __init__(
        self,
        repo_root: Path,
        runner: CommandRunner | None = None,
        *,
        binary: str = "gh",
    ) -> None:
        self.repo_root = repo_root
        self.runner = runner or CommandRunner()
        self.binary = binary

    def find_open_pr_for_branch(
        self, branch: str, *, base: str | None = None
    ) -> PullRequest | None:
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

    def create_pr(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PullRequest:
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
