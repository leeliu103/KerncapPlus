SHELL := /bin/bash

BASE_MAKEFILE ?= Makefile
CLANG ?= /opt/rocm/lib/llvm/bin/clang++
KERNCAP ?= kerncap

REFERENCE_DIR ?= reference
VARIANT_DIR ?= variant
DEBUG_DIR ?= debug

REFERENCE_ASM ?= $(REFERENCE_DIR)/kernel.s
REFERENCE_LL ?= $(REFERENCE_DIR)/kernel.ll
PASS_LOG ?= $(DEBUG_DIR)/llvm-passes.log
VARIANT_ASM ?= $(VARIANT_DIR)/variant.s
VARIANT_HSACO ?= $(VARIANT_DIR)/variant.hsaco
N ?= 50

include $(BASE_MAKEFILE)

.PHONY: export-asm assemble-asm validate-asm bench-asm

# Reuse the kerncap-generated HIP compile line so this file stays portable
# across extracted folders. We only swap the final output mode/file.
define RECOMPILE_CMD
$$( $(MAKE) -s -f $(BASE_MAKEFILE) -n recompile | awk 'BEGIN{p=0} /clang\+\+|hipcc/{p=1} p{printf "%s ", $$0} END{print ""}' | sed 's/[[:space:]]*\\[[:space:]]*/ /g' )
endef

export-asm: $(REFERENCE_ASM) $(REFERENCE_LL) $(PASS_LOG)
	@mkdir -p "$(dir $(VARIANT_ASM))"
	@if [[ ! -f "$(VARIANT_ASM)" ]]; then \
		cp "$(REFERENCE_ASM)" "$(VARIANT_ASM)"; \
		echo "Seeded $(VARIANT_ASM) from $(REFERENCE_ASM)"; \
	else \
		echo "Keeping existing $(VARIANT_ASM)"; \
	fi

$(REFERENCE_ASM): kernel_variant.cpp vfs.yaml $(BASE_MAKEFILE)
	@mkdir -p "$(dir $@)"
	@cmd="$(RECOMPILE_CMD)"; \
	if [[ -z "$$cmd" ]]; then \
		echo "Could not recover the recompile command from $(BASE_MAKEFILE)"; \
		exit 1; \
	fi; \
	cmd="$$(printf '%s\n' "$$cmd" | sed -E 's/[[:space:]]--no-gpu-bundle-output([[:space:]]|$$)/ -S /g; s@[[:space:]]-o[[:space:]][^[:space:]]+[[:space:]]*$$@ -o $(CURDIR)/$@@')"; \
	echo "$$cmd"; \
	eval "$$cmd"

$(REFERENCE_LL): kernel_variant.cpp vfs.yaml $(BASE_MAKEFILE)
	@mkdir -p "$(dir $@)"
	@cmd="$(RECOMPILE_CMD)"; \
	if [[ -z "$$cmd" ]]; then \
		echo "Could not recover the recompile command from $(BASE_MAKEFILE)"; \
		exit 1; \
	fi; \
	cmd="$$(printf '%s\n' "$$cmd" | sed -E 's/[[:space:]]--no-gpu-bundle-output([[:space:]]|$$)/ -S -emit-llvm /g; s@[[:space:]]-o[[:space:]][^[:space:]]+[[:space:]]*$$@ -o $(CURDIR)/$@@')"; \
	echo "$$cmd"; \
	eval "$$cmd"

$(PASS_LOG): kernel_variant.cpp vfs.yaml $(BASE_MAKEFILE)
	@mkdir -p "$(dir $@)"
	@cmd="$(RECOMPILE_CMD)"; \
	if [[ -z "$$cmd" ]]; then \
		echo "Could not recover the recompile command from $(BASE_MAKEFILE)"; \
		exit 1; \
	fi; \
	cmd="$$(printf '%s\n' "$$cmd" | sed -E 's/[[:space:]]--no-gpu-bundle-output([[:space:]]|$$)/ -S /g; s@[[:space:]]-o[[:space:]][^[:space:]]+[[:space:]]*$$@ -o /dev/null@') -mllvm -print-after-all"; \
	echo "$$cmd > $(CURDIR)/$@ 2>&1"; \
	eval "$$cmd" >"$(CURDIR)/$@" 2>&1

$(VARIANT_ASM):
	@mkdir -p "$(dir $@)"
	@if [[ -f "$(REFERENCE_ASM)" ]]; then \
		cp "$(REFERENCE_ASM)" "$@"; \
		echo "Seeded $@ from $(REFERENCE_ASM)"; \
	else \
		echo "Missing $(REFERENCE_ASM). Run 'make -f Makefile.asm export-asm' first."; \
		exit 1; \
	fi

assemble-asm: $(VARIANT_HSACO)

$(VARIANT_HSACO): $(VARIANT_ASM)
	@mkdir -p "$(dir $@)"
	@echo "$(CLANG) -target amdgcn-amd-amdhsa -mcpu=$(GPU_ARCH) -x assembler $(VARIANT_ASM) -o $(VARIANT_HSACO)"
	@$(CLANG) -target amdgcn-amd-amdhsa -mcpu=$(GPU_ARCH) -x assembler $(VARIANT_ASM) -o $(VARIANT_HSACO)

validate-asm: $(VARIANT_HSACO)
	$(KERNCAP) validate . --hsaco $(VARIANT_HSACO)

bench-asm: $(VARIANT_HSACO)
	$(KERNCAP) replay . --hsaco $(VARIANT_HSACO) --iterations $(N)
