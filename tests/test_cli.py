import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner

from xhs_workbench.cli import cli
from xhs_workbench.models import CollectionRun, RunStatus
from xhs_workbench.renderer import BundlePaths
from xhs_workbench.xhs_bridge import BridgeResponse


def test_cli_help_exposes_only_visible_read_only_commands() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert set(cli.commands) == {
        "safe-status",
        "login",
        "collect-search",
        "collect-account",
        "extension-install",
        "extension-status",
        "extension-uninstall",
    }
    for forbidden in ("like", "favorite", "follow", "comment", "publish", "post", "delete"):
        assert forbidden not in result.output.lower()


def test_default_environment_does_not_install_upstream_xhs_console_script() -> None:
    scripts = {entry.name for entry in importlib.metadata.entry_points(group="console_scripts")}
    assert "xhs-workbench" in scripts
    assert "xhs" not in scripts


class _FakeCollector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, Path]] = []

    def collect_search(self, keyword: str, limit: int, output_dir: Path) -> CollectionRun:
        self.calls.append(("search", keyword, limit, output_dir))
        return _run()

    def collect_account(self, profile_url: str, limit: int, output_dir: Path) -> CollectionRun:
        self.calls.append(("account", profile_url, limit, output_dir))
        return _run()


def _run() -> CollectionRun:
    return CollectionRun(
        run_id="safe_run_1",
        mode="search",
        input_summary="safe",
        requested_count=2,
        actual_count=0,
        started_at=datetime(2026, 8, 29, tzinfo=UTC),
        finished_at=datetime(2026, 8, 29, tzinfo=UTC),
        status=RunStatus.COMPLETE,
        notes=[],
    )


def test_collect_search_uses_user_output_root_and_prints_only_safe_summary(
    monkeypatch, tmp_path: Path
) -> None:
    import xhs_workbench.cli as cli_module

    collector = _FakeCollector()
    seen: dict[str, Path] = {}

    monkeypatch.setattr(cli_module, "create_collector", lambda **_kwargs: collector)
    monkeypatch.setattr(cli_module, "new_run_id", lambda: "safe_run_1")

    def write(run: CollectionRun, output: Path) -> BundlePaths:
        seen["output"] = output
        return BundlePaths(output / "results.json", output / "index.html")

    monkeypatch.setattr(cli_module, "write_result_bundle", write)
    root = tmp_path / "obsidian-output"
    result = CliRunner().invoke(
        cli,
        ["collect-search", "--keyword", "private query", "--limit", "2", "--output", str(root)],
    )

    assert result.exit_code == 0
    line = json.loads(result.output)
    assert line == {
        "run_id": "safe_run_1",
        "status": "complete",
        "results_json": str((root / "safe_run_1" / "results.json").resolve()),
        "results_html": str((root / "safe_run_1" / "index.html").resolve()),
    }
    assert "private query" not in result.output
    assert collector.calls == [("search", "private query", 2, root / "safe_run_1")]
    assert seen["output"] == root / "safe_run_1"


def test_collect_command_uses_safe_failed_output_and_nonzero_exit(
    monkeypatch, tmp_path: Path
) -> None:
    import xhs_workbench.cli as cli_module

    monkeypatch.setattr(cli_module, "create_collector", lambda **_kwargs: _FakeCollector())
    monkeypatch.setattr(cli_module, "new_run_id", lambda: "safe_run_1")
    monkeypatch.setattr(
        cli_module,
        "write_result_bundle",
        lambda run, output: BundlePaths(output / "results.json", output / "index.html"),
    )
    failed = _run().model_copy(update={"status": RunStatus.FAILED, "error_code": "adapter_failure"})
    monkeypatch.setattr(_FakeCollector, "collect_search", lambda *_args: failed)

    result = CliRunner().invoke(
        cli,
        ["collect-search", "--keyword", "safe query", "--output", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert json.loads(result.output) == {"error_code": "adapter_failure"}
    assert str(tmp_path) not in result.output


def test_collect_limit_rejects_values_outside_one_to_ten() -> None:
    result = CliRunner().invoke(cli, ["collect-search", "--keyword", "safe", "--limit", "11"])

    assert result.exit_code != 0
    assert json.loads(result.output) == {"error_code": "invalid_request"}


def test_extension_install_reports_a_safe_output_directory_error_without_creating_state(
    monkeypatch, tmp_path: Path
) -> None:
    """A missing output directory needs an actionable finite error, not a generic install failure."""
    import xhs_workbench.extension_install as installer

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(installer, "_home_directory", lambda: home)
    missing_output = tmp_path / "does-not-exist"

    result = CliRunner().invoke(cli, ["extension-install", "--output", str(missing_output)])

    assert result.exit_code != 0
    assert json.loads(result.output) == {"error_code": "extension_output_invalid"}
    assert not (home / ".config/xhs-workbench/extension.json").exists()
    assert str(missing_output) not in result.output


def test_cli_parse_errors_never_echo_a_secret_argument_or_usage() -> None:
    secret = "xsec" + "_token=LEAK"
    cases = [
        ["collect-search", "--keyword", "safe", secret],
        ["collect-search", "--keyword", "safe", "--unknown-option", secret],
        ["collect-search", "--keyword", "safe", "--limit", "web" + "_session=LEAK"],
        ["unknown-command", secret],
    ]

    for args in cases:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code != 0
        assert json.loads(result.output) == {"error_code": "invalid_request"}
        assert secret not in result.output
        assert "usage:" not in result.output.lower()


def test_collect_rejects_unsafe_inputs_and_output_before_creating_a_run(
    monkeypatch, tmp_path: Path
) -> None:
    import xhs_workbench.cli as cli_module

    def collector_must_not_run(**_kwargs: object) -> _FakeCollector:
        raise AssertionError("collector must not run for unsafe CLI input")

    monkeypatch.setattr(cli_module, "create_collector", collector_must_not_run)
    runner = CliRunner()
    unsafe_keyword = "web" + "_session=LEAK"
    unsafe_profile = "https://www.xiaohongshu.com/user/profile/account_a?xsec" + "_token=LEAK"
    unsafe_output = tmp_path / ("web" + "_session=LEAK")

    keyword = runner.invoke(
        cli, ["collect-search", "--keyword", unsafe_keyword, "--output", str(tmp_path / "keyword")]
    )
    profile = runner.invoke(
        cli,
        ["collect-account", "--profile-url", unsafe_profile, "--output", str(tmp_path / "profile")],
    )
    output = runner.invoke(
        cli, ["collect-search", "--keyword", "safe", "--output", str(unsafe_output)]
    )

    for result in (keyword, profile, output):
        assert result.exit_code != 0
        assert json.loads(result.output) == {"error_code": "invalid_request"}
    assert not (tmp_path / "keyword").exists()
    assert not (tmp_path / "profile").exists()
    assert not unsafe_output.exists()


def test_auth_commands_print_only_auth_status_or_safe_error(monkeypatch) -> None:
    import xhs_workbench.cli as cli_module

    class Adapter:
        def auth_status(self) -> BridgeResponse:
            return BridgeResponse(status="complete", payload={"auth_status": "authenticated"})

        def login(self) -> BridgeResponse:
            return BridgeResponse(status="failed", payload={}, error_code="auth_failed")

    monkeypatch.setattr(cli_module, "create_adapter", Adapter)

    status = CliRunner().invoke(cli, ["safe-status"])
    login = CliRunner().invoke(cli, ["login"])

    assert json.loads(status.output) == {"auth_status": "authenticated"}
    assert login.exit_code != 0
    assert json.loads(login.output) == {"error_code": "auth_failed"}


def test_auth_command_prints_profile_busy_without_profile_or_process_details(monkeypatch) -> None:
    """A new finite adapter error must remain safe to expose at the CLI boundary."""
    import xhs_workbench.cli as cli_module

    class Adapter:
        def auth_status(self) -> BridgeResponse:
            return BridgeResponse(status="failed", payload={}, error_code="profile_busy")

    monkeypatch.setattr(cli_module, "create_adapter", Adapter)

    result = CliRunner().invoke(cli, ["safe-status"])

    assert result.exit_code != 0
    assert json.loads(result.output) == {"error_code": "profile_busy"}
    assert ".local/auth" not in result.output
    assert "pid" not in result.output.lower()


def test_collect_command_converts_factory_os_errors_to_a_safe_error(
    monkeypatch, tmp_path: Path
) -> None:
    import xhs_workbench.cli as cli_module

    monkeypatch.setattr(cli_module, "new_run_id", lambda: "safe_run_1")

    def raise_os_error(**_kwargs: object) -> _FakeCollector:
        raise OSError("private filesystem detail")

    monkeypatch.setattr(cli_module, "create_collector", raise_os_error)
    result = CliRunner().invoke(
        cli, ["collect-search", "--keyword", "private query", "--output", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert json.loads(result.output) == {"error_code": "upstream_error"}
    assert "private filesystem detail" not in result.output
