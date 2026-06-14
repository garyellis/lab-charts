from __future__ import annotations

from lab_charts.plumbing.commands import CommandRunner


class Kubectl:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def create_namespace(self, namespace: str) -> None:
        self.runner.run(["kubectl", "create", "namespace", namespace], check=False)

    def wait_workloads_ready(self, namespace: str, timeout: str = "10m") -> None:
        for kind in ("deployment", "statefulset", "daemonset"):
            listing = self.runner.run(
                [
                    "kubectl", "-n", namespace, "get", kind,
                    "-o", "jsonpath={.items[*].metadata.name}",
                ],
                check=False,
            )
            for name in listing.stdout.split():
                self.runner.run(
                    [
                        "kubectl", "-n", namespace, "rollout", "status",
                        f"{kind}/{name}", f"--timeout={timeout}",
                    ],
                    capture=False,
                )

    def diagnostics(self, namespace: str) -> str:
        sections: list[str] = []
        commands = [
            ("pods", ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"]),
            ("events", ["kubectl", "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"]),
        ]
        for title, args in commands:
            result = self.runner.run(args, check=False)
            sections.append(f"## {title}\n{result.stdout}{result.stderr}")
        return "\n\n".join(sections)
