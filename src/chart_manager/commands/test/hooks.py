"""Host-side execution of compiled cluster-test profile hooks."""

from __future__ import annotations

import logging
from pathlib import Path

from chart_manager.commands.test.models import LifecycleAction
from chart_manager.plumbing.commands import CommandRunner, redact
from chart_manager.plumbing.duration import parse_duration
from chart_manager.plumbing.errors import CommandTimeout, ExternalCommandError

_LOG = logging.getLogger(__name__)

#: Stderr lines kept in a failed hook's error.
_STDERR_TAIL_LINES = 20


class ClusterTestHookRunner:
    """Run hook actions from the repository root against one cluster."""

    def __init__(
        self,
        root: Path,
        *,
        runner: CommandRunner,
        kube_context: str,
        cluster_name: str,
    ) -> None:
        self.root = root.resolve()
        self.runner = runner
        self.kube_context = kube_context
        self.cluster_name = cluster_name

    def run(self, action: LifecycleAction) -> None:
        """Run the hook; raise ExternalCommandError on a non-zero exit or timeout."""
        phase = action.kind.value.removeprefix("hook-")
        target = action.target
        env = {
            "CHART_MANAGER_HOOK_PHASE": phase,
            "CHART_MANAGER_ROOT": str(self.root),
            "CHART_MANAGER_CHART": target.chart,
            "CHART_MANAGER_CHART_PATH": str(action.chart_path),
            "CHART_MANAGER_PROFILE": target.profile or "",
            "CHART_MANAGER_RELEASE": target.release or "",
            "CHART_MANAGER_NAMESPACE": target.namespace or "",
            "CHART_MANAGER_KUBE_CONTEXT": self.kube_context,
            "CHART_MANAGER_CLUSTER_NAME": self.cluster_name,
        }
        timeout = action.timeout or "10m"
        command = redact(action.command)
        _LOG.info("running %s hook for %s/%s: %s", phase, target.chart, target.profile, command)
        try:
            result = self.runner.run(
                action.command,
                cwd=self.root,
                check=False,
                timeout=parse_duration(timeout),
                env=env,
            )
        except CommandTimeout as exc:
            raise CommandTimeout(
                f"{phase} hook timed out after {timeout}: {command}{_tail(exc.stderr)}",
                stderr=exc.stderr,
            ) from exc
        # Logged verbatim: hiding sensitive output is the hook script's job.
        _LOG.debug(
            "%s hook output: %s\nstdout:\n%s\nstderr:\n%s",
            phase,
            command,
            result.stdout.rstrip(),
            result.stderr.rstrip(),
        )
        if result.returncode != 0:
            raise ExternalCommandError(
                f"{phase} hook exited {result.returncode}: {command}{_tail(result.stderr)}",
                stderr=result.stderr,
                returncode=result.returncode,
            )


def _tail(stderr: str) -> str:
    lines = stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]
    return "".join(f"\n{line}" for line in lines)


__all__ = ["ClusterTestHookRunner"]
