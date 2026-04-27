# KerncapPlus

KerncapPlus is a thin user-facing facade over `kerncap` for an ASM-first GPU kernel optimization loop.

The goal is simple:

- capture one real kernel dispatch from a real workload
- export reference ASM and LLVM IR
- let an agent modify `variant/variant.s` directly
- assemble, validate, and bench the edited ASM against the same captured input
- once a good ASM change is found, reflect it back into the kernel source

KerncapPlus hides the lower-level extracted-folder workflow behind six commands:

- `kerncap-plus list`
- `kerncap-plus capture`
- `kerncap-plus assemble`
- `kerncap-plus validate`
- `kerncap-plus bench-baseline`
- `kerncap-plus bench`

## Installation

KerncapPlus depends on a patched `kerncap` from IntelliKit.

Install KerncapPlus:

```bash
git clone https://github.com/leeliu103/KerncapPlus.git
cd KerncapPlus
./scripts/setup.sh
```

KerncapPlus must be installed with `./scripts/setup.sh`. Direct
`pip install .` or `pip install -e .` is not supported because KerncapPlus
requires a patched, pinned IntelliKit `kerncap` checkout that emits
`workspace.json` metadata for source-backed ASM workspaces.

## Command Model

The workflow is always:

1. `list` to see what kernels the workload actually launches
2. `capture` to create an ASM-editable workspace
3. edit `variant/variant.s`
4. `assemble`
5. `validate`
6. optionally `bench-baseline` to measure captured baseline performance
7. `bench`

## 1. List

```bash
kerncap-plus list --cmd './build/bin/llama-bench -m /models/model.gguf -p 512 -n 32 -ngl 99'
```

Parameter meaning:

- `--cmd`: the real application command to profile

What it does:

- runs the workload under profiling
- prints the kernels ranked by execution time
- helps you choose the kernel name or substring to capture

## 2. Capture

```bash
kerncap-plus capture \
  --kernel 'mul_mat_q<(ggml_type)39, 128, true>' \
  --cmd './build/bin/llama-bench -m /models/model.gguf -p 512 -n 32 -ngl 99' \
  --source-dir ./ggml/src \
  --workspace ./mmvq_g39_128_true
```

Parameter meaning:

- `--kernel`: the exact kernel name or substring to isolate
- `--cmd`: the real application command to rerun for capture
- `--source-dir`: the source tree root that contains the original HIP kernel source
- `--workspace`: output folder for the prepared workspace

Omitted behavior:

- if `--workspace` is omitted, KerncapPlus uses:

```text
$(pwd)/<sanitized-kernel-name>
```

Example:

- current directory: `/tmp/run`
- kernel: `gemm_kernel`
- default workspace: `/tmp/run/gemm_kernel`

Safety rule:

- if the target workspace already exists, `capture` fails instead of reusing or overwriting it

What `capture` creates:

```text
<workspace>/
  capture/
  reference/module.s
  reference/kernel.s
  reference/kernel.ll
  debug/llvm-passes.log
  variant/variant.s
  variant/merged_module.s
  Makefile
  Makefile.asm
```

Only `variant/variant.s` is meant to be edited. KerncapPlus extracts the
captured kernel symbol into that file, then splices it back into the full
assembly module before assembling.

- `reference/module.s`: full AMDGCN assembly generated from the original source
- `reference/kernel.s`: read-only AMDGCN assembly for the captured kernel symbol
- `reference/kernel.ll`: read-only LLVM IR for the captured kernel symbol
- `debug/llvm-passes.log`: `-print-after-all` dump filtered to the captured symbol
- `variant/variant.s`: editable AMDGCN assembly for the captured kernel symbol
- `variant/merged_module.s`: generated full module; assembled into `variant/variant.hsaco`

After `capture`, edit:

```text
<workspace>/variant/variant.s
```

## 3. Assemble

```bash
kerncap-plus assemble <workspace>
```

Parameter meaning:

- `<workspace>`: the workspace created by `capture`

What it does:

- regenerates `variant/merged_module.s` by replacing the captured symbol in `reference/module.s` with `variant/variant.s`
- assembles the generated `variant/merged_module.s`
- produces `variant/variant.hsaco`

## 4. Validate

```bash
kerncap-plus validate <workspace>
```

Parameter meaning:

- `<workspace>`: the workspace created by `capture`

What it does:

- replays the captured baseline kernel
- replays `variant/variant.hsaco`
- compares outputs using the same captured real-world input and memory snapshot

Use this after every ASM change to check correctness.

## 5. Bench Baseline

```bash
kerncap-plus bench-baseline <workspace> -n 10
```

Parameter meaning:

- `<workspace>`: the workspace created by `capture`
- `-n`, `--iterations`: replay iterations; default is `50`

What it does:

- replays the captured baseline HSACO
- reports baseline timing without assembling or using `variant/variant.hsaco`

Use this to measure the original captured-kernel performance for comparison.

## 6. Bench

```bash
kerncap-plus bench <workspace> -n 10
```

Parameter meaning:

- `<workspace>`: the workspace created by `capture`
- `-n`, `--iterations`: replay iterations; default is `50`

What it does:

- replays `variant/variant.hsaco`
- reports timing for the edited ASM variant

Use this after `validate` to compare performance.

## Minimal Example

```bash
kerncap-plus list --cmd './my_app --args'

kerncap-plus capture \
  --kernel 'my_kernel' \
  --cmd './my_app --args' \
  --source-dir ./src

# edit ./my_kernel/variant/variant.s

kerncap-plus assemble ./my_kernel
kerncap-plus validate ./my_kernel
kerncap-plus bench-baseline ./my_kernel -n 50
kerncap-plus bench ./my_kernel -n 50
```

## Why This Repo Exists

`kerncap` already gives a strong source-backed extraction flow. KerncapPlus exists to make one specific workflow clean:

- let an agent optimize at the ASM level first
- keep the input fixed by using a real captured workload
- measure correctness and performance on every iteration
- use the winning ASM diff as the basis for reflecting the improvement back into kernel source

This keeps the external interface small while still reusing Kerncap underneath.
