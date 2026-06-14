from __future__ import annotations

from pathlib import Path

from lab_charts.plumbing.commands import CommandRunner
from lab_charts.plumbing.errors import ExternalCommandError


class Git:
    def __init__(self, root: Path, runner: CommandRunner | None = None) -> None:
        self.root = root
        self.runner = runner or CommandRunner()

    def is_repository(self) -> bool:
        result = self.runner.run(["git", "rev-parse", "--show-toplevel"], cwd=self.root, check=False)
        return result.returncode == 0

    def changed_charts(self, base: str = "origin/main") -> list[str]:
        if not self.is_repository():
            raise ExternalCommandError("not a git repository; changed chart detection requires git metadata")
        result = self.runner.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=self.root)
        charts: set[str] = set()
        for line in result.stdout.splitlines():
            parts = Path(line).parts
            if len(parts) >= 2 and parts[0] == "charts":
                charts.add(parts[1])
        return sorted(charts)
