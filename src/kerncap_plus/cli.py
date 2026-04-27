"""Command-line interface for KerncapPlus."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import click
from kerncap import Kerncap

from kerncap_plus.core import (
    KerncapPlusError,
    ensure_new_workspace,
    ensure_workspace_exists,
    extract_replay_json,
    install_makefile_asm,
    parse_cmd,
    resolve_workspace,
    run_make_target,
    variant_hsaco_path,
    verify_source_backed_workspace,
)
from kerncap_plus.asm_artifacts import WorkspaceExportError, export_workspace


def _run_capture(kernel: str, cmd: str, source_dir: Path, workspace: Path, dispatch: int) -> None:
    kc = Kerncap()
    kc.extract(
        kernel_name=kernel,
        cmd=parse_cmd(cmd),
        source_dir=str(source_dir),
        output=str(workspace),
        language="hip",
        dispatch=dispatch,
    )
    verify_source_backed_workspace(workspace)
    install_makefile_asm(workspace, overwrite=True)
    export_workspace(workspace)


def _assemble_workspace(workspace: Path) -> Path:
    install_makefile_asm(workspace, overwrite=False)
    run_make_target(workspace, "assemble-asm")
    hsaco = variant_hsaco_path(workspace)
    if not hsaco.exists():
        raise KerncapPlusError(f"Expected variant HSACO was not produced: {hsaco}")
    return hsaco


@click.group()
def main() -> None:
    """KerncapPlus facade for source-backed HIP ASM workflows."""


@main.command("list")
@click.option("--cmd", required=True, help="Application command to profile.")
def list_kernels(cmd: str) -> None:
    """Run kerncap profile and preserve its original CLI output."""
    try:
        argv = parse_cmd(cmd)
        proc = subprocess.run(
            ["kerncap", "profile", "--", *argv],
            check=False,
        )
    except KerncapPlusError as exc:
        raise click.ClickException(str(exc)) from exc
    except FileNotFoundError as exc:
        raise click.ClickException("`kerncap` executable not found in PATH") from exc

    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


@main.command("capture")
@click.option("--kernel", required=True, help="Kernel name or substring to capture.")
@click.option("--cmd", required=True, help="Application command to run for capture.")
@click.option(
    "--source-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Source tree root used to locate the kernel source.",
)
@click.option(
    "--workspace",
    type=click.Path(path_type=Path),
    default=None,
    help="Output workspace directory. Defaults to $(pwd)/<sanitized-kernel-name>.",
)
@click.option(
    "--dispatch",
    default=-1,
    show_default=True,
    type=int,
    help="Dispatch index to capture (-1 means first match).",
)
def capture_kernel(kernel: str, cmd: str, source_dir: Path, workspace: Path | None, dispatch: int) -> None:
    """Capture a kernel and leave a ready-to-edit ASM workspace."""
    target_workspace = resolve_workspace(kernel, workspace)
    try:
        ensure_new_workspace(target_workspace)
        click.echo(f"workspace: {target_workspace}")
        _run_capture(kernel, cmd, source_dir, target_workspace, dispatch)
    except (KerncapPlusError, WorkspaceExportError) as exc:
        if target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - thin CLI wrapper
        if target_workspace.exists():
            shutil.rmtree(target_workspace, ignore_errors=True)
        raise click.ClickException(str(exc)) from exc

    click.echo("READY")
    click.echo(f"workspace: {target_workspace}")
    click.echo(f"edit: {target_workspace / 'variant' / 'variant.s'}")
    click.echo(f"module: {target_workspace / 'reference' / 'module.s'}")
    click.echo(f"merged: {target_workspace / 'variant' / 'merged_module.s'}")
    click.echo(f"reference: {target_workspace / 'reference' / 'kernel.s'}")
    click.echo(f"ir: {target_workspace / 'reference' / 'kernel.ll'}")
    click.echo(f"passes: {target_workspace / 'debug' / 'llvm-passes.log'}")
    click.echo("next:")
    click.echo(f"  kerncap-plus assemble {target_workspace}")
    click.echo(f"  kerncap-plus validate {target_workspace}")
    click.echo(f"  kerncap-plus bench-baseline {target_workspace} -n 50")
    click.echo(f"  kerncap-plus bench {target_workspace} -n 50")


@main.command("assemble")
@click.argument("workspace", type=click.Path(exists=True, file_okay=False, path_type=Path))
def assemble_workspace(workspace: Path) -> None:
    """Assemble the replay-safe full module into variant/variant.hsaco."""
    try:
        resolved = ensure_workspace_exists(workspace)
        hsaco = _assemble_workspace(resolved)
    except (KerncapPlusError, WorkspaceExportError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"assembled: {hsaco}")


@main.command("validate")
@click.argument("workspace", type=click.Path(exists=True, file_okay=False, path_type=Path))
def validate_workspace(workspace: Path) -> None:
    """Validate variant output against the captured baseline."""
    try:
        resolved = ensure_workspace_exists(workspace)
        hsaco = _assemble_workspace(resolved)
        result = Kerncap().validate(str(resolved), hsaco=str(hsaco))
    except (KerncapPlusError, WorkspaceExportError) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - thin CLI wrapper
        raise click.ClickException(str(exc)) from exc

    for line in result.details:
        click.echo(line)
    if not result.passed:
        raise click.ClickException("Validation failed.")
    click.echo("PASS")


@main.command("bench-baseline")
@click.argument("workspace", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-n", "--iterations", default=50, show_default=True, type=int, help="Replay iterations.")
def bench_baseline_workspace(workspace: Path, iterations: int) -> None:
    """Replay the captured baseline HSACO and report timing."""
    try:
        resolved = ensure_workspace_exists(workspace)
        proc = subprocess.run(
            [
                "kerncap",
                "replay",
                str(resolved),
                "--iterations",
                str(iterations),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise KerncapPlusError((proc.stdout + proc.stderr).strip())
        payload = extract_replay_json(proc.stdout)
    except KerncapPlusError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - thin CLI wrapper
        raise click.ClickException(str(exc)) from exc

    kernel = payload["kernel"]["name"]
    timing = payload["timing"]
    click.echo(f"workspace: {resolved}")
    click.echo(f"kernel: {kernel}")
    click.echo(f"iterations: {payload['execution']['iterations']}")
    click.echo(
        "timing_us: "
        f"avg={timing['average']:.3f} min={timing['min']:.3f} max={timing['max']:.3f}"
    )


@main.command("bench")
@click.argument("workspace", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-n", "--iterations", default=50, show_default=True, type=int, help="Replay iterations.")
def bench_workspace(workspace: Path, iterations: int) -> None:
    """Replay the variant HSACO and report timing."""
    try:
        resolved = ensure_workspace_exists(workspace)
        hsaco = _assemble_workspace(resolved)
        proc = subprocess.run(
            [
                "kerncap",
                "replay",
                str(resolved),
                "--hsaco",
                str(hsaco),
                "--iterations",
                str(iterations),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise KerncapPlusError((proc.stdout + proc.stderr).strip())
        payload = extract_replay_json(proc.stdout)
    except (KerncapPlusError, WorkspaceExportError) as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - thin CLI wrapper
        raise click.ClickException(str(exc)) from exc

    kernel = payload["kernel"]["name"]
    timing = payload["timing"]
    click.echo(f"workspace: {resolved}")
    click.echo(f"kernel: {kernel}")
    click.echo(f"iterations: {payload['execution']['iterations']}")
    click.echo(
        "timing_us: "
        f"avg={timing['average']:.3f} min={timing['min']:.3f} max={timing['max']:.3f}"
    )


@main.command("export-workspace", hidden=True)
@click.argument("workspace", type=click.Path(exists=True, file_okay=False, path_type=Path))
def export_workspace_command(workspace: Path) -> None:
    """Regenerate symbol-scoped reference outputs for an extracted workspace."""
    try:
        resolved = ensure_workspace_exists(workspace)
        install_makefile_asm(resolved, overwrite=True)
        export_workspace(resolved)
    except (KerncapPlusError, WorkspaceExportError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"workspace: {resolved}")
    click.echo(f"edit: {resolved / 'variant' / 'variant.s'}")
    click.echo(f"module: {resolved / 'reference' / 'module.s'}")
    click.echo(f"merged: {resolved / 'variant' / 'merged_module.s'}")
    click.echo(f"reference: {resolved / 'reference' / 'kernel.s'}")
    click.echo(f"ir: {resolved / 'reference' / 'kernel.ll'}")
    click.echo(f"passes: {resolved / 'debug' / 'llvm-passes.log'}")


if __name__ == "__main__":
    main()
