"""Command-line entry points for the read-only visible-result workbench."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import click

from xhs_workbench.collector import VisibleCollector
from xhs_workbench.extension_install import (
    ExtensionInstallError,
    extension_status,
    install_extension,
    uninstall_extension,
)
from xhs_workbench.renderer import BundlePaths, BundleWriteError, write_result_bundle
from xhs_workbench.xhs_adapter import IsolatedXhsAdapter
from xhs_workbench.xhs_bridge import BridgeErrorCode, BridgeResponse, is_safe_retained_text

_SAFE_ERROR_CODES = frozenset(
    {code.value for code in BridgeErrorCode}
    | {
        "adapter_failure",
        "invalid_bridge_payload",
        "invalid_output_dir",
        "output_conflict",
        "media_unavailable",
        "profile_unavailable",
        "output_write_failed",
        "invalid_local_asset",
        "unsafe_bundle",
        "invalid_request",
        "extension_output_invalid",
    }
)


class _LimitType(click.ParamType[int]):
    name = "limit"

    def convert(
        self, value: object, param: click.Parameter | None, ctx: click.Context | None
    ) -> int:
        try:
            parsed = int(str(value))
        except (TypeError, ValueError):
            self.fail("limit_invalid", param, ctx)
        if not 1 <= parsed <= 10:
            self.fail("limit_out_of_range", param, ctx)
        return parsed


LIMIT = _LimitType()


class SafeGroup(click.Group):
    """Emit only a finite JSON error for parsing failures, while preserving help output."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,
    ) -> Any:
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                **extra,
            )
        except click.UsageError:
            _echo({"error_code": "invalid_request"})
            if standalone_mode:
                raise SystemExit(2) from None
            raise click.exceptions.Exit(2) from None
        if isinstance(result, int) and result != 0:
            if standalone_mode:
                raise SystemExit(result)
            raise click.exceptions.Exit(result)
        return result


def create_adapter() -> IsolatedXhsAdapter:
    """Build the production adapter; tests replace this factory."""
    return IsolatedXhsAdapter()


def create_collector(*, run_id_factory: Callable[[], str]) -> VisibleCollector:
    """Build a collector with the CLI-preallocated run identifier."""
    return VisibleCollector(create_adapter(), run_id_factory=run_id_factory)


def new_run_id() -> str:
    """Return an opaque filename-safe identifier without input-derived data."""
    return uuid.uuid4().hex


@click.group(cls=SafeGroup)
def cli() -> None:
    """Read-only Xiaohongshu research workbench."""


@cli.command("safe-status")
def safe_status() -> None:
    """Report only the safe local authentication state."""
    try:
        response = create_adapter().auth_status()
    except Exception:  # noqa: BLE001 - authentication diagnostics must not reach stdout.
        _fail("upstream_error")
    _auth_response(response)


@cli.command("login")
def login() -> None:
    """Request browser authentication without printing browser/session data."""
    try:
        response = create_adapter().login()
    except Exception:  # noqa: BLE001 - browser diagnostics must not reach stdout.
        _fail("upstream_error")
    _auth_response(response)


@cli.command("collect-search")
@click.option("--keyword", required=True, type=str)
@click.option("--limit", default=10, show_default=True, type=LIMIT)
@click.option(
    "--output", default=Path(".local/mvp"), type=click.Path(path_type=Path, file_okay=False)
)
def collect_search(keyword: str, limit: int, output: Path) -> None:
    """Collect a read-only keyword snapshot and save local results."""
    _collect("search", keyword, limit, output)


@cli.command("collect-account")
@click.option("--profile-url", required=True, type=str)
@click.option("--limit", default=10, show_default=True, type=LIMIT)
@click.option(
    "--output", default=Path(".local/mvp"), type=click.Path(path_type=Path, file_okay=False)
)
def collect_account(profile_url: str, limit: int, output: Path) -> None:
    """Collect one read-only account snapshot and save local results."""
    _collect("account", profile_url, limit, output)


@cli.command("extension-install")
@click.option("--output", required=True, type=click.Path(path_type=Path, file_okay=False))
def extension_install(output: Path) -> None:
    """Install the packaged extension without opening Chrome or a profile."""
    try:
        _echo(install_extension(output))
    except (OSError, TypeError, ValueError, ExtensionInstallError) as error:
        _fail(_extension_install_error_code(error))


@cli.command("extension-status")
def extension_status_command() -> None:
    """Check only local installer integrity without opening Chrome or a profile."""
    _echo(extension_status())


@cli.command("extension-uninstall")
def extension_uninstall() -> None:
    """Remove only unchanged installer-owned extension files."""
    try:
        _echo(uninstall_extension())
    except (OSError, TypeError, ValueError, ExtensionInstallError):
        _fail("extension_uninstall_refused")


def _auth_response(response: BridgeResponse) -> None:
    if response.status == "complete" and response.error_code is None:
        auth_status = response.payload.get("auth_status")
        if auth_status in {"authenticated", "action_required"}:
            _echo({"auth_status": auth_status})
            return
    _fail(_error_code(response.error_code.value if response.error_code is not None else None))


def _collect(mode: str, value: str, limit: int, output_root: Path) -> None:
    if not is_safe_retained_text(value) or not is_safe_retained_text(str(output_root)):
        _fail("invalid_request")
    run_id = _reserve_run_id(output_root)
    if run_id is None:
        _fail("output_conflict")
    output_dir = output_root / run_id
    try:
        collector = create_collector(run_id_factory=lambda: run_id)
        run = (
            collector.collect_search(value, limit, output_dir)
            if mode == "search"
            else collector.collect_account(value, limit, output_dir)
        )
        paths = write_result_bundle(run, output_dir)
    except BundleWriteError as error:
        _fail(_error_code(str(error)))
    except (TypeError, ValueError):
        _fail("invalid_request")
    except Exception:  # noqa: BLE001 - collector diagnostics must not reach stdout.
        _fail("upstream_error")
    if run.run_id != run_id:
        _fail("invalid_request")
    if run.status.value == "failed":
        _fail(_error_code(run.error_code))
    _echo(_success_payload(run.run_id, run.status.value, paths))


def _reserve_run_id(output_root: Path) -> str | None:
    for _attempt in range(100):
        run_id = new_run_id()
        if isinstance(run_id, str) and run_id and not (output_root / run_id).exists():
            return run_id
    return None


def _success_payload(run_id: str, status: str, paths: BundlePaths) -> dict[str, object]:
    return {
        "run_id": run_id,
        "status": status,
        "results_json": str(paths.json_path.resolve()),
        "results_html": str(paths.html_path.resolve()),
    }


def _error_code(value: str | None) -> str:
    return value if value in _SAFE_ERROR_CODES else "upstream_error"


def _extension_install_error_code(error: Exception) -> str:
    """Expose only the one setup action that is safe and useful to disclose."""
    if isinstance(error, ExtensionInstallError) and str(error).startswith("output root"):
        return "extension_output_invalid"
    return "extension_install_failed"


def _echo(value: Mapping[str, object]) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _fail(error_code: str) -> NoReturn:
    _echo({"error_code": error_code})
    raise click.exceptions.Exit(1)
