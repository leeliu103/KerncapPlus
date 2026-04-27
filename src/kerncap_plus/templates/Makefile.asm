SHELL := /bin/bash

BASE_MAKEFILE ?= Makefile
CLANG ?= /opt/rocm/lib/llvm/bin/clang++
KERNCAP ?= kerncap
PYTHON ?= python3

REFERENCE_DIR ?= reference
VARIANT_DIR ?= variant
DEBUG_DIR ?= debug

REFERENCE_MODULE_ASM ?= $(REFERENCE_DIR)/module.s
REFERENCE_ASM ?= $(REFERENCE_DIR)/kernel.s
REFERENCE_LL ?= $(REFERENCE_DIR)/kernel.ll
PASS_LOG ?= $(DEBUG_DIR)/llvm-passes.log
VARIANT_ASM ?= $(VARIANT_DIR)/variant.s
MERGED_MODULE_ASM ?= $(VARIANT_DIR)/merged_module.s
VARIANT_HSACO ?= $(VARIANT_DIR)/variant.hsaco
EXPORT_HELPER ?= .kerncap_plus/asm_artifacts.py
N ?= 50

include $(BASE_MAKEFILE)

.PHONY: export-asm assemble-asm validate-asm bench-asm

export-asm: $(REFERENCE_MODULE_ASM) $(REFERENCE_ASM) $(REFERENCE_LL) $(PASS_LOG)

$(REFERENCE_MODULE_ASM) $(REFERENCE_ASM) $(REFERENCE_LL) $(PASS_LOG): kernel_variant.cpp vfs.yaml $(BASE_MAKEFILE) $(EXPORT_HELPER)
	@$(PYTHON) "$(EXPORT_HELPER)" export "$(CURDIR)" --base-makefile "$(BASE_MAKEFILE)"

$(VARIANT_ASM): $(REFERENCE_ASM)
	@mkdir -p "$(dir $@)"
	@if [[ -f "$@" ]]; then \
		echo "Keeping existing $@"; \
	elif [[ -f "$(REFERENCE_ASM)" ]]; then \
		cp "$(REFERENCE_ASM)" "$@"; \
		echo "Seeded $@ from $(REFERENCE_ASM)"; \
	else \
		echo "Missing $(REFERENCE_ASM). Run 'make -f Makefile.asm export-asm' first."; \
		exit 1; \
	fi

$(MERGED_MODULE_ASM): $(REFERENCE_MODULE_ASM) $(VARIANT_ASM) $(EXPORT_HELPER)
	@mkdir -p "$(dir $@)"
	@$(PYTHON) "$(EXPORT_HELPER)" materialize "$(CURDIR)" --base-makefile "$(BASE_MAKEFILE)"

assemble-asm: $(VARIANT_HSACO)

$(VARIANT_HSACO): $(MERGED_MODULE_ASM)
	@mkdir -p "$(dir $@)"
	@echo "$(CLANG) -target amdgcn-amd-amdhsa -mcpu=$(GPU_ARCH) -x assembler $(MERGED_MODULE_ASM) -o $(VARIANT_HSACO)"
	@$(CLANG) -target amdgcn-amd-amdhsa -mcpu=$(GPU_ARCH) -x assembler "$(MERGED_MODULE_ASM)" -o "$(VARIANT_HSACO)"

validate-asm: $(VARIANT_HSACO)
	$(KERNCAP) validate . --hsaco $(VARIANT_HSACO)

bench-asm: $(VARIANT_HSACO)
	$(KERNCAP) replay . --hsaco $(VARIANT_HSACO) --iterations $(N)
