"""Workspace ASM artifact helpers for captured-kernel editing workflows.

This module is intentionally self-contained so KerncapPlus can copy it into an
extracted workspace and keep `make -f Makefile.asm ...` portable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ASM_SECTION_RE = r"^\s*\.section\s+\.text\."
DEFINE_RE = re.compile(r"^\s*define\b")


class WorkspaceExportError(RuntimeError):
    """User-facing export/materialization failure."""


@dataclass(frozen=True)
class KernelSymbol:
    mangled: str
    demangled: str


@dataclass(frozen=True)
class WorkspaceManifest:
    symbol: KernelSymbol
    compile_dir: Path
    compile_argv: tuple[str, ...]


def export_workspace(workspace: Path, base_makefile: str = "Makefile") -> None:
    """Generate full-module references plus symbol-scoped inspection outputs."""
    workspace = workspace.expanduser().resolve()
    manifest = load_workspace_manifest(workspace, base_makefile)
    tools = resolve_toolchain(list(manifest.compile_argv))

    reference_dir = workspace / "reference"
    debug_dir = workspace / "debug"
    variant_dir = workspace / "variant"
    reference_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.mkdir(parents=True, exist_ok=True)
    variant_dir.mkdir(parents=True, exist_ok=True)

    reference_module = reference_dir / "module.s"
    reference_kernel = reference_dir / "kernel.s"
    reference_ll = reference_dir / "kernel.ll"
    pass_log = debug_dir / "llvm-passes.log"
    variant_asm = variant_dir / "variant.s"
    legacy_symbol_json = debug_dir / "kernel-symbol.json"
    legacy_replay_asm = variant_dir / "replay.s"

    with tempfile.TemporaryDirectory(prefix="kerncap-plus-export-", dir=workspace) as tmp:
        tmpdir = Path(tmp)

        dump_cmd = build_compile_command(
            manifest,
            workspace,
            tmpdir / "device.hsaco",
            extra_flags=["--save-temps=obj"],
        )
        run_command(dump_cmd, cwd=manifest.compile_dir)

        device_bc = find_single_artifact(tmpdir, ".bc")
        device_asm = find_single_artifact(tmpdir, ".s")
        shutil.copy2(device_asm, reference_module)
        extract_asm_block(device_asm, manifest.symbol.mangled, reference_kernel)
        extract_llvm_ir(device_bc, manifest.symbol.mangled, reference_ll, tools)

        pass_cmd = build_compile_command(
            manifest,
            workspace,
            tmpdir / "passdump.hsaco",
            extra_flags=[
                "-mllvm",
                "-print-after-all",
                "-mllvm",
                f"-filter-print-funcs={manifest.symbol.mangled}",
            ],
        )
        run_command(
            pass_cmd,
            cwd=manifest.compile_dir,
            stdout_path=pass_log,
            stderr_path=pass_log,
        )

    if not variant_asm.exists():
        shutil.copy2(reference_kernel, variant_asm)

    legacy_symbol_json.unlink(missing_ok=True)
    legacy_replay_asm.unlink(missing_ok=True)
    materialize_variant_asm(workspace, base_makefile)


def materialize_variant_asm(workspace: Path, base_makefile: str = "Makefile") -> None:
    """Render a replay-safe full-module assembly from the edited kernel slice."""
    workspace = workspace.expanduser().resolve()
    manifest = load_workspace_manifest(workspace, base_makefile)

    reference_module = workspace / "reference" / "module.s"
    reference_kernel = workspace / "reference" / "kernel.s"
    variant_asm = workspace / "variant" / "variant.s"
    merged_module_asm = workspace / "variant" / "merged_module.s"

    if not reference_module.is_file():
        raise WorkspaceExportError(
            f"Missing {reference_module}. Run `make -f Makefile.asm export-asm` first."
        )

    if not variant_asm.exists():
        if not reference_kernel.is_file():
            raise WorkspaceExportError(
                f"Missing {variant_asm} and {reference_kernel}. Run export-asm first."
            )
        variant_asm.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reference_kernel, variant_asm)

    module_text = reference_module.read_text(encoding="utf-8", errors="ignore")
    variant_text = variant_asm.read_text(encoding="utf-8", errors="ignore")
    replacement_text = extract_asm_block_text(variant_text, manifest.symbol.mangled)
    replay_text = replace_asm_block_text(module_text, manifest.symbol.mangled, replacement_text)

    merged_module_asm.parent.mkdir(parents=True, exist_ok=True)
    merged_module_asm.write_text(replay_text, encoding="utf-8")


def build_compile_command(
    manifest: WorkspaceManifest,
    workspace: Path,
    output_path: Path,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build the exact workspace recompile command for a new output path."""
    cmd = [arg for arg in manifest.compile_argv if not arg.startswith("--save-temps")]
    cmd.extend(
        [
            "-ivfsoverlay",
            str((workspace / "vfs.yaml").resolve()),
            "--cuda-device-only",
            "--no-gpu-bundle-output",
        ]
    )
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.extend(["-o", str(output_path)])
    return cmd


def load_workspace_manifest(workspace: Path, base_makefile: str = "Makefile") -> WorkspaceManifest:
    """Load captured symbol and source compile metadata from workspace.json."""
    _ = base_makefile
    manifest_path = workspace / "workspace.json"
    if not manifest_path.is_file():
        raise WorkspaceExportError(
            f"Missing {manifest_path}. Run `scripts/setup.sh`, then recapture the workspace."
        )

    payload = load_json_file(manifest_path)
    mangled = str(payload.get("mangled_name", "")).strip()
    demangled = (
        str(payload.get("demangled_name", "")).strip()
        or str(payload.get("kernel_name", "")).strip()
        or mangled
    )
    compile_dir_raw = str(payload.get("compile_dir", "")).strip()
    compile_argv_raw = payload.get("compile_argv")
    if (
        not mangled
        or not compile_dir_raw
        or not isinstance(compile_argv_raw, list)
        or not compile_argv_raw
    ):
        raise WorkspaceExportError(
            f"{manifest_path} does not contain mangled_name, compile_dir, and compile_argv."
        )

    return WorkspaceManifest(
        symbol=KernelSymbol(mangled=mangled, demangled=demangled),
        compile_dir=Path(compile_dir_raw).expanduser().resolve(),
        compile_argv=tuple(str(arg) for arg in compile_argv_raw),
    )


def load_json_file(path: Path) -> dict:
    """Read a JSON file with a workspace-friendly error message."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkspaceExportError(f"Could not read JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceExportError(f"Could not parse JSON file: {path}") from exc


def resolve_toolchain(cmd: list[str]) -> dict[str, str]:
    """Resolve LLVM helper binaries next to the recovered compiler when possible."""
    if not cmd:
        raise WorkspaceExportError("Recovered recompile command is empty.")

    compiler = cmd[0]
    compiler_path = Path(compiler)
    tool_dir = compiler_path.parent if compiler_path.exists() else None

    def find_tool(name: str) -> str:
        candidates: list[Path] = []
        if tool_dir is not None:
            candidates.append(tool_dir / name)
        candidates.append(Path("/opt/rocm/llvm/bin") / name)
        candidates.append(Path("/opt/rocm/lib/llvm/bin") / name)

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        found = shutil.which(name)
        if found:
            return found

        raise WorkspaceExportError(f"Required tool `{name}` not found in PATH.")

    return {
        "llvm_dis": find_tool("llvm-dis"),
        "llvm_extract": find_tool("llvm-extract"),
    }


def run_command(
    cmd: list[str],
    cwd: Path,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> None:
    """Run a subprocess, optionally redirecting stdout / stderr to files."""
    kwargs: dict[str, object] = {
        "cwd": cwd,
        "env": {**os.environ, "PWD": str(cwd)},
        "text": True,
    }

    stdout_handle = None
    stderr_handle = None
    try:
        if stdout_path is None:
            kwargs["stdout"] = subprocess.PIPE
        else:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = stdout_path.open("w", encoding="utf-8")
            kwargs["stdout"] = stdout_handle

        if stderr_path is None:
            kwargs["stderr"] = subprocess.PIPE
        elif stderr_path == stdout_path and stdout_handle is not None:
            kwargs["stderr"] = stdout_handle
        else:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_handle = stderr_path.open("w", encoding="utf-8")
            kwargs["stderr"] = stderr_handle

        proc = subprocess.run(cmd, **kwargs)
    finally:
        if stderr_handle is not None:
            stderr_handle.close()
        if stdout_handle is not None:
            stdout_handle.close()

    if proc.returncode != 0:
        stdout = getattr(proc, "stdout", "") or ""
        stderr = getattr(proc, "stderr", "") or ""
        detail = (stdout + stderr).strip() or "subprocess failed"
        raise WorkspaceExportError(detail)


def find_single_artifact(directory: Path, suffix: str) -> Path:
    """Locate the freshest saved-temp device artifact with the given suffix."""
    candidates = sorted(
        directory.glob(f"*amdgcn-amd-amdhsa*{suffix}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise WorkspaceExportError(
            f"Could not find a device `{suffix}` artifact under {directory}."
        )
    return candidates[0]


def extract_llvm_ir(
    device_bc: Path,
    symbol: str,
    output_path: Path,
    tools: dict[str, str],
) -> None:
    """Extract a single kernel definition as textual LLVM IR."""
    tmp_bc = output_path.with_suffix(".bc.tmp")
    try:
        proc = subprocess.run(
            [tools["llvm_extract"], f"--func={symbol}", str(device_bc), "-o", str(tmp_bc)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            proc_dis = subprocess.run(
                [tools["llvm_dis"], str(tmp_bc), "-o", str(output_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc_dis.returncode == 0:
                return
    finally:
        tmp_bc.unlink(missing_ok=True)

    proc_full = subprocess.run(
        [tools["llvm_dis"], str(device_bc), "-o", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc_full.returncode != 0:
        detail = proc_full.stderr.strip() or proc_full.stdout.strip() or "llvm-dis failed"
        raise WorkspaceExportError(detail)

    output_path.write_text(filter_single_function_ir(proc_full.stdout, symbol), encoding="utf-8")


def filter_single_function_ir(module_ir: str, symbol: str) -> str:
    """Keep one function body while preserving the rest of the module skeleton."""
    lines = module_ir.splitlines(keepends=True)
    output: list[str] = []
    in_function = False
    keep_function = False
    symbol_re = re.compile(r"^\s*define\b.*@" + re.escape(symbol) + r"\(")

    for line in lines:
        if not in_function:
            if DEFINE_RE.match(line):
                in_function = True
                keep_function = bool(symbol_re.match(line))
                if keep_function:
                    output.append(line)
            else:
                output.append(line)
            continue

        if keep_function:
            output.append(line)

        if line.strip() == "}":
            in_function = False
            keep_function = False

    if not any(symbol in line for line in output):
        raise WorkspaceExportError(
            f"Target symbol `{symbol}` was not found in the generated LLVM IR."
        )

    return "".join(output)


def find_asm_block_range(lines: list[str], symbol: str) -> tuple[int, int]:
    """Locate the line range for one kernel's assembly region."""
    start_pat = re.compile(ASM_SECTION_RE + re.escape(symbol) + r"\b")
    next_section_pat = re.compile(ASM_SECTION_RE)

    start = None
    for index, line in enumerate(lines):
        if start is None:
            if start_pat.search(line):
                start = index
            continue
        if next_section_pat.search(line) and not start_pat.search(line):
            return start, index

    if start is None:
        raise WorkspaceExportError(
            f"Target symbol `{symbol}` was not found in the generated AMDGCN assembly."
        )
    return start, len(lines)


def extract_asm_block_text(module_text: str, symbol: str) -> str:
    """Return the selected kernel's assembly region as text."""
    lines = module_text.splitlines(keepends=True)
    start, end = find_asm_block_range(lines, symbol)
    return "".join(lines[start:end])


def extract_asm_block(amdgcn_s: Path, symbol: str, out_path: Path) -> None:
    """Write the selected kernel's assembly region to a file."""
    module_text = amdgcn_s.read_text(encoding="utf-8", errors="ignore")
    out_path.write_text(extract_asm_block_text(module_text, symbol), encoding="utf-8")


def replace_asm_block_text(module_text: str, symbol: str, replacement_text: str) -> str:
    """Replace one kernel region inside a full module assembly."""
    module_lines = module_text.splitlines(keepends=True)
    start, end = find_asm_block_range(module_lines, symbol)
    replacement = replacement_text
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    replacement_lines = replacement.splitlines(keepends=True)
    return "".join(module_lines[:start] + replacement_lines + module_lines[end:])


def main(argv: list[str] | None = None) -> int:
    """Workspace-local command-line entrypoint."""
    parser = argparse.ArgumentParser(description="Workspace-local KerncapPlus export helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Regenerate reference outputs")
    export_parser.add_argument("workspace", type=Path)
    export_parser.add_argument("--base-makefile", default="Makefile")

    materialize_parser = subparsers.add_parser(
        "materialize",
        help="Regenerate the replay-safe full-module assembly",
    )
    materialize_parser.add_argument("workspace", type=Path)
    materialize_parser.add_argument("--base-makefile", default="Makefile")

    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            export_workspace(args.workspace, base_makefile=args.base_makefile)
        else:
            materialize_variant_asm(args.workspace, base_makefile=args.base_makefile)
    except WorkspaceExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
