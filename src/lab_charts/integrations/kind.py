from __future__ import annotations

from lab_charts.plumbing.commands import CommandRunner


class Kind:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def clusters(self) -> list[str]:
        result = self.runner.run(["kind", "get", "clusters"], check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def ensure_cluster(self, name: str) -> None:
        if name in self.clusters():
            return
        self.runner.run(["kind", "create", "cluster", "--name", name])
