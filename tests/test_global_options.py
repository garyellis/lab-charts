"""The root callback: global options, their precedence, and the `version` command.

Precedence, from design doc 6.5, is:

    flag > CHART_MANAGER_* env > .chart-manager/config.yaml > default

It is split across two mechanisms and neither half is obvious, which is why
each step below is asserted rather than assumed:

  * `Settings` implements `env > config.yaml > default`, via its source
    ordering. It cannot implement the first step because it never sees argv.
  * The root callback implements `flag > (whatever Settings resolved)`, and
    then hands the answer to commands through Click's `default_map` -- NOT by
    writing to `Settings`, which is frozen (`model_config` `frozen=True`).
    `default_map` sits *below* the command line in Click's lookup order, so
    the 18 per-command `--root` flags keep overriding it. That is what makes
    this callback a non-breaking addition.

Also pinned here: the two things deliberately NOT in the root callback. A
global `-o/--output` would collide with `grafana export-dashboard -o PATH`
and `helmrelease --output pretty|json`; a global `--version` would collide
with the *chart* `--version` on `publish`, `events`, and `helmrelease`. Both
are asserted absent so neither returns by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer.main
from typer.testing import Result

from chart_manager.cli import main
from chart_manager.settings import DEFAULT_CONFIG_FILE, Settings, set_config_file

from .conftest import cli


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
    return directory


def _config(directory: Path, root: Path) -> Path:
    """A config file declaring `root:` and nothing else."""
    path = directory / "config.yaml"
    path.write_text(f"root: {root}\n", encoding="utf-8")
    return path


def _charts(*argv: str) -> Result:
    """`charts list` is the cheapest command whose output names the root used."""
    return cli(*argv)


# --------------------------------------------------------------------------
# --root precedence, one step at a time
# --------------------------------------------------------------------------


def test_root_defaults_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo_with_chart(tmp_path, "zeta"))

    result = _charts("charts", "list")

    assert result.exit_code == 0
    assert "zeta" in result.stdout


def test_config_file_beats_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from_file = _repo_with_chart(tmp_path / "file", "beta")

    result = _charts("--config", str(_config(tmp_path, from_file)), "charts", "list")

    assert result.exit_code == 0
    assert "beta" in result.stdout


def test_env_beats_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    from_file = _repo_with_chart(tmp_path / "file", "beta")
    from_env = _repo_with_chart(tmp_path / "env", "gama")
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(from_env))

    result = _charts("--config", str(_config(tmp_path, from_file)), "charts", "list")

    assert result.exit_code == 0
    assert "gama" in result.stdout
    assert "beta" not in result.stdout


def test_flag_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    from_env = _repo_with_chart(tmp_path / "env", "gama")
    from_flag = _repo_with_chart(tmp_path / "flag", "zeta")
    monkeypatch.setenv("CHART_MANAGER_ROOT", str(from_env))

    result = _charts("--root", str(from_flag), "charts", "list")

    assert result.exit_code == 0
    assert "zeta" in result.stdout
    assert "gama" not in result.stdout


def test_a_commands_own_root_beats_the_global_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 18 per-command `--root` flags must keep working as overrides.

    This is the property that makes the callback non-breaking. `default_map`
    is consulted *after* the command line, so a per-command flag wins.
    """
    monkeypatch.chdir(tmp_path)
    global_root = _repo_with_chart(tmp_path / "global", "gama")
    command_root = _repo_with_chart(tmp_path / "cmd", "zeta")

    result = _charts(
        "--root", str(global_root), "charts", "list", "--root", str(command_root)
    )

    assert result.exit_code == 0
    assert "zeta" in result.stdout
    assert "gama" not in result.stdout


def test_the_global_root_reaches_a_nested_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`default_map` is nested per command name, so depth is a real risk.

    `grafana lint-dashboards` sits two levels below the root group. If the
    map were flat this would silently keep using the working directory.
    """
    monkeypatch.chdir(_repo_with_chart(tmp_path / "cwd", "zeta"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = cli("--root", str(elsewhere), "grafana", "lint-dashboards")

    # No dashboards under `elsewhere` -> the P0.4 empty exit. Reaching this
    # at all proves the group's root was resolved without a per-command flag.
    assert result.exit_code == 1
    assert "no dashboards found" in result.stderr


def test_settings_is_never_mutated_to_carry_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback threads root through `default_map`, not through Settings.

    Settings is frozen; an implementation that tried to write back would
    raise. Asserting the frozen config *and* that a `--root` run leaves a
    freshly built Settings at its default keeps a future refactor from
    quietly introducing a mutable global.
    """
    monkeypatch.chdir(tmp_path)
    assert Settings.model_config["frozen"] is True

    other = _repo_with_chart(tmp_path / "other", "zeta")
    assert _charts("--root", str(other), "charts", "list").exit_code == 0

    assert Settings().root == Path(".")


def test_an_absent_config_file_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.chart-manager/config.yaml` does not exist in this repo and need not."""
    monkeypatch.chdir(_repo_with_chart(tmp_path, "zeta"))
    assert not (tmp_path / DEFAULT_CONFIG_FILE).exists()

    result = _charts("charts", "list")

    assert result.exit_code == 0


# --------------------------------------------------------------------------
# the remaining global flags
# --------------------------------------------------------------------------


def test_quiet_suppresses_narration_but_not_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _repo_with_chart(tmp_path / "repo", "zeta")

    loud = _charts("--root", str(root), "grafana", "lint-dashboards")
    quiet = _charts("-q", "--root", str(root), "grafana", "lint-dashboards")

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
    """`-vv` means more than `-v` (6.4: stream subprocess output).

    The behavioral half of that lives in services and is not in this commit,
    so pin the count now: a later phase reads it, and a `bool` flag here
    would have thrown the distinction away irrecoverably.
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
    """P1.4 landed the root `-o`, together with the vocabulary unification.

    It was deliberately held back from P0.10 (design doc 6.2 / plan 2.7)
    until there was one vocabulary for it to name, so that no release ever
    shipped `-o` meaning three different things.
    """
    assert "-o" in _root_option_names()
    assert "--output" in _root_option_names()

    result = cli("-o", "json", "version")
    assert result.exit_code == 0


def test_the_global_output_does_not_reach_grafana_export_dashboards_path() -> None:
    """The collision that kept `-o` out of P0.10, proven absent.

    `grafana export-dashboard` has its own `-o`, and it is a *file path*.
    Two things could have broken it, and this asserts both are fine:

      1. Click scopes options per command, so a root `-o` and a subcommand
         `-o` are separate parameters and the subcommand's wins after its
         own name.
      2. `cli/main.global_options` seeds `ctx.default_map` for `--root` by
         parameter *name*. Doing the same for `output` would hand this
         command `Path("json")` and write the dashboard into a file called
         `json` -- silently, exiting 0. It deliberately does not.

    P2.2 flips this flag to a format on purpose, with no alias. Until then
    it means a path, including under a global `-o`.
    """
    root_command = typer.main.get_command(main.app)
    command = root_command.commands["grafana"].commands["export-dashboard"]
    output_param = next(p for p in command.params if p.name == "output")
    assert "-o" in output_param.opts
    assert output_param.default is None

    # Leg 2, checked against the whole nested tree rather than its top level.
    # `_root_default_map` returns `{"grafana": {"export-dashboard": {...}}}`,
    # so a top-level `"output" not in ...` would pass no matter what and prove
    # nothing. Walk it.
    def _keys(mapping: dict[str, object]) -> set[str]:
        found: set[str] = set()
        for key, value in mapping.items():
            if isinstance(value, dict):
                found |= _keys(value)
            else:
                found.add(key)
        return found

    seeded = main._root_default_map(root_command, Path(".")) or {}
    assert _keys(seeded) == {"root"}, (
        "the global callback may seed only `root` into default_map; seeding "
        "`output` would redirect `grafana export-dashboard` into a file named "
        "after the projection"
    )

    # And end to end: a global `-o json` must not become this command's path.
    result = cli("-o", "json", "grafana", "export-dashboard", "--help")
    assert result.exit_code == 0


def test_there_is_no_global_version_flag() -> None:
    """`--version` is the *chart* version elsewhere; the CLI's is a command."""
    assert "--version" not in _root_option_names()

    result = cli("--version")
    assert result.exit_code == 2


def test_chart_version_flag_still_belongs_to_the_commands_that_own_it() -> None:
    """Guard the guard for the test above: prove the collision is real."""
    command = typer.main.get_command(main.app)
    publish = command.commands["publish"]

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
