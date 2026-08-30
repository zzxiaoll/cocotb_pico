SHELL := /bin/bash

SIM ?= verilator
TOPLEVEL_LANG ?= verilog
TOPLEVEL := picorv32_cocotb_top
MODULE := test_picorv32

VENV ?= $(HOME)/venv
PYTHON_BIN := $(VENV)/bin/python
COCOTB_CONFIG := $(VENV)/bin/cocotb-config
export PATH := $(VENV)/bin:$(PATH)

TOOLCHAIN_PREFIX ?= riscv64-unknown-elf-
CC := $(TOOLCHAIN_PREFIX)gcc
OBJCOPY := $(TOOLCHAIN_PREFIX)objcopy
OBJDUMP := $(TOOLCHAIN_PREFIX)objdump
SPIKE ?= spike

CUSTOM_IRQ ?= 0
COMPRESSED_ISA ?= 1

ifeq ($(filter $(CUSTOM_IRQ),0 1),)
$(error CUSTOM_IRQ must be 0 or 1)
endif
ifeq ($(filter $(COMPRESSED_ISA),0 1),)
$(error COMPRESSED_ISA must be 0 or 1)
endif

ifeq ($(COMPRESSED_ISA),1)
FIRMWARE_ISA := rv32imc
RTL_ISA_DEFINES := -DCOMPRESSED_ISA
else
FIRMWARE_ISA := rv32im
RTL_ISA_DEFINES :=
endif

ifeq ($(CUSTOM_IRQ),0)
IRQ_DEFINES := -DDISABLE_CUSTOM_IRQ
else
IRQ_DEFINES :=
endif

BUILD_TAG := irq$(CUSTOM_IRQ)_c$(COMPRESSED_ISA)
BUILD_DIR := build/$(BUILD_TAG)
SIM_BUILD := sim_build/$(BUILD_TAG)

PICORV32_RTL ?= ../picorv32.v
WRAPPER_RTL := picorv32_cocotb_top.v
VERILOG_SOURCES := $(abspath $(PICORV32_RTL)) $(abspath $(WRAPPER_RTL))

FIRMWARE_C_SRCS := \
	firmware/print.c \
	firmware/hello.c \
	firmware/sieve.c \
	firmware/mytest.c \
	firmware/multest.c
ifeq ($(CUSTOM_IRQ),1)
FIRMWARE_C_SRCS += firmware/irq.c firmware/stats.c
endif
FIRMWARE_C_OBJS := $(patsubst firmware/%.c,$(BUILD_DIR)/firmware/%.o,$(FIRMWARE_C_SRCS))
START_OBJ := $(BUILD_DIR)/firmware/start.o
TEST_SRCS := $(sort $(wildcard tests/*.S))
TEST_OBJS := $(patsubst tests/%.S,$(BUILD_DIR)/tests/%.o,$(TEST_SRCS))
FIRMWARE_OBJS := $(START_OBJ) $(FIRMWARE_C_OBJS) $(TEST_OBJS)

FIRMWARE_ELF := $(BUILD_DIR)/firmware.elf
FIRMWARE_BIN := $(BUILD_DIR)/firmware.bin
FIRMWARE_HEX := $(BUILD_DIR)/firmware.hex
FIRMWARE_MAP := $(BUILD_DIR)/firmware.map
FIRMWARE_DIS := $(BUILD_DIR)/firmware.dis

PROGRAM_BASE := 0x10000
SPIKE_MEMORY := 0x10000:0x20000,0x10000000:0x1000,0x20000000:0x1000
SPIKE_LOG := $(abspath logs/spike_commit.log)
SPIKE_NORMALIZED_LOG := $(abspath logs/spike_normalized.log)
COMPARE_REPORT := $(abspath logs/compare_report.txt)
COMPARE_SCRIPT := compare_commit_logs.py

COMMON_ARCH_FLAGS := -mabi=ilp32 -march=$(FIRMWARE_ISA)
COMMON_FREESTANDING_FLAGS := $(COMMON_ARCH_FLAGS) -ffreestanding -nostdlib
GCC_WARNS := -Werror -Wall -Wextra -Wshadow -Wundef -Wpointer-arith
GCC_WARNS += -Wcast-qual -Wcast-align -Wwrite-strings -Wredundant-decls
GCC_WARNS += -Wstrict-prototypes -Wmissing-prototypes -pedantic

COMPILE_ARGS += -Wno-fatal -DRISCV_FORMAL $(RTL_ISA_DEFINES) $(IRQ_DEFINES)
CUSTOM_COMPILE_DEPS := $(PICORV32_RTL) $(WRAPPER_RTL)
CUSTOM_SIM_DEPS := $(FIRMWARE_HEX)

export PYTHONPATH := $(CURDIR):$(PYTHONPATH)
export FIRMWARE := $(abspath $(FIRMWARE_HEX))
export RVFI_LOG ?= $(abspath logs/cocotb_commit.log)
export MAX_CYCLES ?= 1000000
export CUSTOM_IRQ

.DEFAULT_GOAL := run

.PHONY: run firmware disassemble spike compare check-inputs check-spike clean-firmware

run: compare

firmware: $(FIRMWARE_HEX)

disassemble: $(FIRMWARE_DIS)

spike: sim check-spike
	@mkdir -p logs
	@commit_count="$$(grep -c ' trap=0 ' "$(RVFI_LOG)")"; \
	test "$$commit_count" -gt 0 || { echo "no non-trap RVFI commits in $(RVFI_LOG)"; exit 1; }; \
	$(SPIKE) --isa=$(FIRMWARE_ISA) --priv=m --pc=$(PROGRAM_BASE) --disable-dtb \
		--instructions="$$commit_count" -m$(SPIKE_MEMORY) \
		-l --log-commits --log="$(SPIKE_LOG)" "$(FIRMWARE_ELF)"

compare: spike
	$(PYTHON_BIN) $(COMPARE_SCRIPT) \
		--rtl "$(RVFI_LOG)" \
		--spike "$(SPIKE_LOG)" \
		--spike-normalized "$(SPIKE_NORMALIZED_LOG)" \
		--report "$(COMPARE_REPORT)"

check-inputs:
	@test -x "$(PYTHON_BIN)" || { echo "missing Python: $(PYTHON_BIN)"; exit 1; }
	@test -x "$(COCOTB_CONFIG)" || { echo "missing cocotb-config: $(COCOTB_CONFIG)"; exit 1; }
	@command -v "$(CC)" >/dev/null || { echo "missing compiler: $(CC)"; exit 1; }
	@test -f "$(PICORV32_RTL)" || { echo "missing RTL: $(PICORV32_RTL)"; exit 1; }

check-spike:
	@test "$(CUSTOM_IRQ)" = "0" || { echo "Spike comparison requires CUSTOM_IRQ=0"; exit 1; }
	@command -v "$(SPIKE)" >/dev/null || { echo "missing Spike: $(SPIKE)"; exit 1; }

sim: check-inputs

$(START_OBJ): firmware/start.S firmware/custom_ops.S
	@mkdir -p $(dir $@)
	$(CC) -c $(COMMON_ARCH_FLAGS) $(IRQ_DEFINES) -o $@ $<

$(BUILD_DIR)/firmware/%.o: firmware/%.c firmware/firmware.h
	@mkdir -p $(dir $@)
	$(CC) -c $(COMMON_ARCH_FLAGS) -Os --std=c99 $(GCC_WARNS) -ffreestanding -nostdlib -o $@ $<

$(BUILD_DIR)/tests/%.o: tests/%.S tests/riscv_test.h tests/test_macros.h
	@mkdir -p $(dir $@)
	$(CC) -c -mabi=ilp32 -march=rv32im -o $@ \
		-DTEST_FUNC_NAME=$* -DTEST_FUNC_TXT='"$*"' -DTEST_FUNC_RET=$*_ret $<

$(FIRMWARE_ELF): $(FIRMWARE_OBJS) firmware/sections.lds
	@mkdir -p $(dir $@)
	$(CC) -Os $(COMMON_FREESTANDING_FLAGS) -o $@ \
		-Wl,--build-id=none,-Bstatic,-T,firmware/sections.lds,-Map,$(FIRMWARE_MAP),--strip-debug \
		$(FIRMWARE_OBJS) -lgcc

$(FIRMWARE_BIN): $(FIRMWARE_ELF)
	$(OBJCOPY) -O binary $< $@

$(FIRMWARE_HEX): $(FIRMWARE_BIN) firmware/makehex.py
	$(PYTHON_BIN) firmware/makehex.py $< 32768 > $@

$(FIRMWARE_DIS): $(FIRMWARE_ELF)
	$(OBJDUMP) -d -M no-aliases,numeric $< > $@

clean-firmware:
	$(RM) -r build logs results.xml __pycache__

clean:: clean-firmware

include $(shell $(COCOTB_CONFIG) --makefiles)/Makefile.sim
