"""Internal helpers for the KerncapPlus CLI."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from importlib import resources


MAKEFILE_NAME = "Makefile.asm"
WORKSPACE_HELPER_NAME = ".kerncap_plus/asm_artifacts.py"
LEGACY_WORKSPACE_HELPER_NAME = ".kerncap_plus/export_workspace.py"


class KerncapPlusError(RuntimeError):
    """User-facing operational error."""


def sanitize_kernel_name(kernel_name: str) -> str:
    """Convert a kernel name into a stable directory slug."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", kernel_name).strip("_").lower()
    return slug or "captured_kernel"


def resolve_workspace(kernel_name: str, workspace: Path | None) -> Path:
    """Return the workspace path for capture."""
    if workspace is not None:
        return workspace.expanduser().resolve()
    return (Path.cwd() / sanitize_kernel_name(kernel_name)).resolve()


def parse_cmd(cmd: str) -> list[str]:
    """Split a user-provided shell command into argv."""
    argv = shlex.split(cmd)
    if not argv:
        raise KerncapPlusError("Command is empty")
    return argv


def ensure_new_workspace(workspace: Path) -> None:
    """Reject reuse of an existing workspace path."""
    if workspace.exists():
        raise KerncapPlusError(
            f"Workspace already exists: {workspace}\n"
            "Use --workspace to choose another path."
        )


def ensure_workspace_exists(workspace: Path) -> Path:
    """Validate a workspace directory path."""
    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise KerncapPlusError(f"Workspace does not exist: {resolved}")
    return resolved


def install_makefile_asm(workspace: Path, overwrite: bool = False) -> Path:
    """Install workspace-managed Makefile.asm support files if needed."""
    dst = workspace / MAKEFILE_NAME
    helper_dst = workspace / WORKSPACE_HELPER_NAME
    legacy_helper_dst = workspace / LEGACY_WORKSPACE_HELPER_NAME

    if overwrite or not dst.exists():
        template = (
            resources.files("kerncap_plus")
            .joinpath("templates")
            .joinpath(MAKEFILE_NAME)
            .read_text(encoding="utf-8")
        )
        dst.write_text(template, encoding="utf-8")

    helper = resources.files("kerncap_plus").joinpath("asm_artifacts.py").read_text(
        encoding="utf-8"
    )

    if overwrite or not helper_dst.exists():
        helper_dst.parent.mkdir(parents=True, exist_ok=True)
        helper_dst.write_text(helper, encoding="utf-8")
        helper_dst.chmod(0o755)

    # Keep the previous hidden helper path working for already-extracted workspaces
    # whose Makefile.asm still points at it.
    if overwrite or not legacy_helper_dst.exists():
        legacy_helper_dst.parent.mkdir(parents=True, exist_ok=True)
        legacy_helper_dst.write_text(helper, encoding="utf-8")
        legacy_helper_dst.chmod(0o755)

    return dst


def verify_source_backed_workspace(workspace: Path) -> None:
    """Ensure the extracted workspace is usable for ASM export."""
    required = [
        workspace / "Makefile",
        workspace / "kernel_variant.cpp",
        workspace / "vfs.yaml",
        workspace / "workspace.json",
    ]
    missing = [str(path.name) for path in required if not path.exists()]
    if missing:
        raise KerncapPlusError(
            "Workspace is not source-backed and compilable.\n"
            f"Missing required files: {', '.join(missing)}"
        )

    manifest_path = workspace / "workspace.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KerncapPlusError(f"Could not read workspace metadata: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise KerncapPlusError(f"Could not parse workspace metadata: {manifest_path}") from exc

    compile_dir = str(payload.get("compile_dir", "")).strip()
    compile_argv = payload.get("compile_argv")
    mangled = str(payload.get("mangled_name", "")).strip()
    if not compile_dir or not isinstance(compile_argv, list) or not compile_argv or not mangled:
        raise KerncapPlusError(
            "workspace.json is missing mangled_name, compile_dir, or compile_argv."
        )


def run_make_target(workspace: Path, target: str, extra_vars: dict[str, str] | None = None) -> str:
    """Run a Makefile.asm target and return captured stdout."""
    cmd = ["make", "-f", MAKEFILE_NAME, target]
    for key, value in (extra_vars or {}).items():
        cmd.append(f"{key}={value}")

    proc = subprocess.run(
        cmd,
        cwd=workspace,
        capture_output=True,
        text=True,
        env={**os.environ, "PWD": str(workspace)},
    )
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        raise KerncapPlusError(
            f"Target failed: {target}\n"
            f"{detail}"
        )
    return proc.stdout.strip()


def variant_hsaco_path(workspace: Path) -> Path:
    """Return the standard variant HSACO path."""
    return workspace / "variant" / "variant.hsaco"


def extract_replay_json(output: str) -> dict[str, Any]:
    """Parse the JSON payload from `kerncap replay --json` output."""
    start = output.find("{")
    if start < 0:
        raise KerncapPlusError("Replay output did not contain JSON")
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError as exc:
        raise KerncapPlusError("Could not parse replay JSON output") from exc
