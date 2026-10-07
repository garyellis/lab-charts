"""The root callback: global options, their precedence, and the `version` command.

Repository-root precedence is:

    CHART_MANAGER_ROOT env > config.yaml > nearest workspace

There is no cwd fallback: an explicit root must hold
`.chart-manager/workspace.yaml` itself, and discovery that finds no marker
is a `WorkspaceNotFoundError` (exit 5). It is split across two mechanisms and
neither half is obvious, which is why each step below is asserted rather
than assumed:

`Settings` implements environment over config-file precedence. Repository
commands then perform nearest-marker discovery; non-repository commands do
not. There is deliberately no CLI `--root` spelling.

Also pinned here: the global `-o/--output` reaches commands through
`ctx.obj` and never through `default_map`, and there is deliberately no
global `--version`, which would collide with the *chart* `--version` on
`publish`, `events`, and `promote`. Both are asserted so neither
property is lost by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner, Result

from chart_manager import main
from chart_manager.cli import _container
from chart_manager.settings import DEFAULT_CONFIG_FILE, Settings, set_config_file

from .conftest import cli, write_workspace


@pytest.fixture(autouse=True)
def _restore_process_state() -> object:
    """Undo the process-wide state the callback sets.

    The callback deliberately mutates module-level things -- the config-file
    location, the consoles' color/quiet flags. That is correct for a process
    with one invocation and wrong for a test session with hundreds.
    """
    saved = (main.console.no_color, main.narration.no_color, main.narration.quiet)
    yield None
    set_config_file(DEFAULT_CONFIG_FILE)
    main.console.no_color, main.narration.no_color, main.narration.quiet = saved


def _repo_with_chart(directory: Path, name: str) -> Path:
    """A minimal repository root whose only chart is `name`."""
    chart = directory / "charts" / name
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n", encoding="utf-8"
    )
    write_workspace(directory)
    return directory


def _config(directory: Path, root: Path) -> Path:
    """A config file declaring `root:` and nothing else."""
    path = directory / "config.yaml"
    path.write_text(f"root: {root}\n", encoding="utf-8")
    return path


def _charts(*argv: str) -> Result:
    """`chart list` is the cheapest command whose output names the root used."""
    return cli(*argv)


# --------------------------------------------------------------------------
# root precedence, one step at a time
# --------------------------------------------------------------------------


def test_root_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo_with_chart(tmp_path, "zeta"))

    result = _charts("chart", "list")

    assert result.exit_code == 0
    assert "zeta" in result.stdout


def test_non_repository_command_never_discovers_a_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("version must not load repository state")

    monkeypatch.setattr(_container, "load_repository_workspace", fail)

    assert cli("version").exit_code == 0


def test_a_repository_command_loads_the_workspace_once_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Container per invocation, so workspace.yaml is parsed once.

    `chart list` asks for the workspace twice -- once to discover the root,
    once to build the catalog. Each `container()` call used to build a fresh
    Container, and with it a fresh workspace memo, so the file was read twice.
    """
    root = _repo_with_chart(tmp_path, "zeta")
    monkeypatch.chdir(root)
    loaded: list[Path] = []
    real = _container.load_repository_workspace

    def counting(path: Path, **kwargs):  # type: ignore[no-untyped-def]
        loaded.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(_container, "load_repository_workspace", counting)

    result = _charts("chart", "list")

    assert result.exit_code == 0, result.output
    assert "zeta" in result.stdout
    assert loaded == [root.resolve()]


def test_config_file_beats_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from_file = _repo_with_chart(tmp_path / "file", "beta")

    result = _charts("--config", str(_config(tmp_path, from_file)), "chart", "list")

    assert result.exit_code == 0
    assert "beta" in result.stdout


def test_env_beats_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from_file = _repo_with_chart(tmp_path / "file", "beta")
    from_env = _repo_with_chart(tmp_path / "env", "gama")
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(from_env))

    result = _charts("--config", str(_config(tmp_path, from_file)), "chart", "list")

    assert result.exit_code == 0
    assert "gama" in result.stdout
    assert "beta" not in result.stdout


@pytest.mark.parametrize(
    "argv",
    [("--root", "/tmp/repo", "chart", "list"), ("chart", "list", "--root", "/tmp/repo")],
)
def test_cli_root_option_is_removed(argv: tuple[str, ...]) -> None:
    result = CliRunner().invoke(main.app, list(argv))

    assert result.exit_code == 2
    assert "No such option: --root" in result.stderr


def test_environment_root_reaches_a_nested_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deeply nested repository command uses the same operator override."""
    monkeypatch.chdir(_repo_with_chart(tmp_path / "cwd", "zeta"))
    elsewhere = tmp_path / "elsewhere"
    write_workspace(elsewhere)
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(elsewhere))

    result = cli("grafana", "dashboard", "lint")

    # No dashboards under `elsewhere` -> the empty exit. Reaching this
    # at all proves the group's root was resolved without a per-command flag.
    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr


def test_settings_remains_frozen_while_carrying_the_operator_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root configuration is source-loaded, never mutated by the CLI."""
    monkeypatch.chdir(tmp_path)
    assert Settings.model_config["frozen"] is True

    other = _repo_with_chart(tmp_path / "other", "zeta")
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(other))
    assert _charts("chart", "list").exit_code == 0

    assert Settings().root == other


def test_an_absent_config_file_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.chart-manager/config.yaml` does not exist in this repo and need not."""
    monkeypatch.chdir(_repo_with_chart(tmp_path, "zeta"))
    assert not (tmp_path / DEFAULT_CONFIG_FILE).exists()

    result = _charts("chart", "list")

    assert result.exit_code == 0


# --------------------------------------------------------------------------
# the remaining global flags
# --------------------------------------------------------------------------


def test_quiet_suppresses_narration_but_not_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _repo_with_chart(tmp_path / "repo", "zeta")
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(root))

    loud = _charts("grafana", "dashboard", "lint")
    quiet = _charts("-q", "grafana", "dashboard", "lint")

    assert "no dashboards found" in loud.stderr
    assert quiet.stderr == ""
    # Silencing narration must not silence failure.
    assert quiet.exit_code == 1


def test_quiet_leaves_the_error_console_alone() -> None:
    """`-q` sets `narration.quiet`; `errors` is a separate console for a reason.

    `main()` reports uncaught domain errors through `errors`. If `-q` had
    silenced that console too, a quiet run would die with no output and no
    explanation.
    """
    assert main.errors is not main.narration
    assert main.errors.stderr is True


def test_no_color_flag_and_NO_COLOR_env_both_disable_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NO_COLOR", raising=False)

    assert _charts("--no-color", "version").exit_code == 0
    assert main.console.no_color is True

    main.console.no_color = False
    monkeypatch.setenv("NO_COLOR", "1")

    assert _charts("version").exit_code == 0
    assert main.console.no_color is True


def test_verbose_raises_the_log_level_and_silence_leaves_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(main, "setup_logging", lambda level, **kw: seen.append(level))

    assert _charts("version").exit_code == 0
    assert seen == []

    assert _charts("-v", "version").exit_code == 0
    assert seen == ["DEBUG"]


def test_verbosity_is_a_count_not_a_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-vv` means more than `-v` (stream subprocess output).

    Pin the count: a `bool` flag here would throw the distinction away
    irrecoverably.
    """
    monkeypatch.chdir(tmp_path)
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(main, "GlobalOptions", lambda **kw: captured.append(kw))

    assert _charts("-v", "version").exit_code == 0
    assert _charts("-vv", "version").exit_code == 0
    assert _charts("version").exit_code == 0

    assert [c["verbosity"] for c in captured] == [1, 2, 0]
    assert [c["quiet"] for c in captured] == [False, False, False]


# --------------------------------------------------------------------------
# the `version` command, and the two flags that must NOT exist
# --------------------------------------------------------------------------


def test_version_command_prints_the_version_on_stdout() -> None:
    result = cli("version")

    assert result.exit_code == 0
    assert result.stdout.strip() == main._package_version()
    assert result.stderr == ""


def _root_option_names() -> set[str]:
    """Every long/short option the root callback declares."""
    command = typer.main.get_command(main.app)
    return {opt for param in command.params for opt in param.opts}


def test_there_is_a_global_output_flag() -> None:
    """The root `-o` names the one output vocabulary every `--output` speaks,
    so `-o` never means three different things.
    """
    assert "-o" in _root_option_names()
    assert "--output" in _root_option_names()

    result = cli("-o", "json", "version")
    assert result.exit_code == 0


def test_root_is_not_exposed_by_any_cli_command() -> None:
    """Repository addressing is environment/config/discovery only."""
    root_command = typer.main.get_command(main.app)

    def _root_parameters(command) -> list[str]:  # type: ignore[no-untyped-def]
        found = [param.name for param in command.params if param.name == "root"]
        for subcommand in (getattr(command, "commands", None) or {}).values():
            found.extend(_root_parameters(subcommand))
        return found

    assert _root_parameters(root_command) == []


def test_no_command_reads_output_as_a_file_path() -> None:
    """`--output` is a format everywhere; a destination file is `--to`.

    `grafana export-dashboard -o PATH` was the last exception, and it is the
    reason the command is now `grafana dashboard export ... --to PATH`. A
    `Path`-typed parameter named `output` anywhere in the tree would mean the
    exception is back -- and under a global `-o json` it would silently write
    into a file called `json`.
    """
    # `param.type.name` rather than an isinstance check against `click.Path`:
    # typer 0.26 vendors click, so there is no importable `click` for a test
    # to name (see the note in `tests/conftest.py`).
    pending = [typer.main.get_command(main.app)]
    offenders: list[str] = []
    while pending:
        command = pending.pop()
        pending.extend(getattr(command, "commands", {}).values())
        for param in command.params:
            if param.name == "output" and getattr(param.type, "name", "") == "path":
                offenders.append(f"{command.name}: {param.opts}")

    assert not offenders, offenders

    export = typer.main.get_command(main.app).commands["grafana"].commands["dashboard"]
    to_param = next(p for p in export.commands["export"].params if p.name == "to")
    assert to_param.opts == ["--to"]


def test_there_is_no_global_version_flag() -> None:
    """`--version` is the *chart* version elsewhere; the CLI's is a command."""
    assert "--version" not in _root_option_names()

    result = cli("--version")
    assert result.exit_code == 2


def test_chart_version_flag_still_belongs_to_the_commands_that_own_it() -> None:
    """Guard the guard for the test above: prove the collision is real."""
    command = typer.main.get_command(main.app)
    publish = command.commands["chart"].commands["publish"]

    assert "--version" in {opt for param in publish.params for opt in param.opts}


def test_upgrade_finalize_parsing_is_unchanged() -> None:
    """FROZEN by `renovate-global.json:5`'s allowlist regex.

    The regex pins the literal command string and flag order, and Renovate
    runs it outside this repo where a parse change fails silently. A root
    callback must not add, rename, or reorder anything it parses.
    """
    command = typer.main.get_command(main.app)
    finalize = command.commands["upgrade-finalize"]
    declared = {opt for param in finalize.params for opt in param.opts}

    # The name and the one flag Renovate types, both literal.
    assert "upgrade-finalize" in command.commands
    assert "--path" in declared

    # Nothing the callback declares may also be a flag on this command: an
    # option name owned by both would change which parser consumes it.
    assert declared.isdisjoint(_root_option_names())

    # The regex allows `--path <value>` and nothing else after the name, so
    # a *required* new flag would break Renovate even though it parses here.
    required = {
        param.name
        for param in finalize.params
        if getattr(param, "required", False)
    }
    assert required <= {"path"}
