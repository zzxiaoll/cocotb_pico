import os
import re
import select
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from compare_commit_logs import (
    Commit,
    SPIKE_MEM_PATTERN,
    SPIKE_PATTERN,
    SPIKE_RD_PATTERN,
    lane_offset,
    packed_store_data,
    rtl_load_address,
)
from test_picorv32 import AxiMemorySlave, CUSTOM_0_OPCODE


SPIKE_PROMPT = b"(spike) "
SPIKE_DISASM_PATTERN = re.compile(
    r"^core\s+\d+:\s+(?P<pc>0x[0-9a-f]+)\s+\((?P<insn>0x[0-9a-f]+)\)"
)


@dataclass(frozen=True)
class SpikeStep:
    pc: int
    insn: int
    commit: Commit | None
    exception: bool
    raw: str


def sample_rtl_commit(dut):
    order = int(dut.rvfi_order.value)
    pc = int(dut.rvfi_pc_rdata.value)
    insn = int(dut.rvfi_insn.value)
    rd_addr_value = int(dut.rvfi_rd_addr.value)
    rd_addr = rd_addr_value or None
    rd_data = int(dut.rvfi_rd_wdata.value) if rd_addr is not None else None
    rmask = int(dut.rvfi_mem_rmask.value)
    wmask = int(dut.rvfi_mem_wmask.value)

    if rmask and wmask:
        raise AssertionError(
            f"RTL commit has both read and write masks at order={order}: "
            f"rmask=0x{rmask:x}, wmask=0x{wmask:x}"
        )

    mem_kind = None
    mem_addr = None
    mem_data = None
    if rmask:
        mem_kind = "load"
        mem_addr = rtl_load_address(
            insn,
            int(dut.rvfi_rs1_rdata.value),
            int(dut.rvfi_mem_addr.value),
            rmask,
        )
    elif wmask:
        mem_kind = "store"
        mem_addr = int(dut.rvfi_mem_addr.value) + lane_offset(wmask)
        mem_data = packed_store_data(wmask, int(dut.rvfi_mem_wdata.value))

    return Commit(
        order=order,
        pc=pc,
        insn=insn,
        rd_addr=rd_addr,
        rd_data=rd_data,
        mem_kind=mem_kind,
        mem_addr=mem_addr,
        mem_data=mem_data,
    )


def parse_spike_step(raw, order):
    disasms = []
    commits = []
    exception = False

    for line in raw.splitlines():
        disasm_match = SPIKE_DISASM_PATTERN.match(line)
        if disasm_match:
            disasms.append(disasm_match)

        commit_match = SPIKE_PATTERN.match(line)
        if commit_match:
            commits.append(commit_match)

        if "exception " in line:
            exception = True

    if len(disasms) != 1:
        raise AssertionError(
            f"Expected one Spike instruction at order={order}, got {len(disasms)}:\n{raw}"
        )
    if len(commits) > 1:
        raise AssertionError(
            f"Expected at most one Spike commit at order={order}, got {len(commits)}:\n{raw}"
        )

    commit = None
    if commits:
        match = commits[0]
        effects = match.group("effects")
        rd_match = SPIKE_RD_PATTERN.search(effects)
        mem_match = SPIKE_MEM_PATTERN.search(effects)

        rd_addr = int(rd_match.group("addr")) if rd_match else None
        rd_data = int(rd_match.group("data"), 16) if rd_match else None
        mem_kind = None
        mem_addr = None
        mem_data = None
        if mem_match:
            mem_addr = int(mem_match.group("addr"), 16)
            if mem_match.group("data") is None:
                mem_kind = "load"
            else:
                mem_kind = "store"
                mem_data = int(mem_match.group("data"), 16)

        commit = Commit(
            order=order,
            pc=int(match.group("pc"), 16),
            insn=int(match.group("insn"), 16),
            rd_addr=rd_addr,
            rd_data=rd_data,
            mem_kind=mem_kind,
            mem_addr=mem_addr,
            mem_data=mem_data,
        )

    disasm = disasms[0]
    return SpikeStep(
        pc=int(disasm.group("pc"), 16),
        insn=int(disasm.group("insn"), 16),
        commit=commit,
        exception=exception,
        raw=raw,
    )


class SpikeStepper:
    def __init__(self, command, timeout):
        self.timeout = timeout
        self.pending = b""
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            self.startup = self._read_until_prompt()
        except BaseException:
            self.close()
            raise

    def _read_until_prompt(self):
        deadline = time.monotonic() + self.timeout
        while True:
            prompt_at = self.pending.find(SPIKE_PROMPT)
            if prompt_at >= 0:
                result = self.pending[:prompt_at]
                self.pending = self.pending[prompt_at + len(SPIKE_PROMPT):]
                return result.decode("utf-8", errors="replace")

            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Spike exited with status {self.process.returncode}:\n"
                    f"{self.pending.decode('utf-8', errors='replace')}"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for the Spike prompt")

            readable, _, _ = select.select(
                [self.process.stderr.fileno()], [], [], remaining
            )
            if not readable:
                raise TimeoutError("Timed out waiting for the Spike prompt")

            chunk = os.read(self.process.stderr.fileno(), 4096)
            if not chunk:
                raise RuntimeError("Spike closed stderr before returning a prompt")
            self.pending += chunk

    def step(self, order):
        if self.process.poll() is not None:
            raise RuntimeError(f"Spike already exited with status {self.process.returncode}")
        self.process.stdin.write(b"\n")
        self.process.stdin.flush()
        return parse_spike_step(self._read_until_prompt(), order)

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write(b"q\n")
                self.process.stdin.flush()
                self.process.wait(timeout=2)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()

        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stderr is not None:
            self.process.stderr.close()


def mismatch_message(reason, recent, rtl_commit, spike_step):
    lines = [reason, "", "Recent matching commits:"]
    lines.extend(recent or ["<none>"])
    lines.extend(
        [
            "",
            f"RTL  : {rtl_commit.line()}",
            f"Spike: {spike_step.commit.line() if spike_step.commit else '<no commit>'}",
            "",
            "Raw Spike step:",
            spike_step.raw,
        ]
    )
    return "\n".join(lines)


@cocotb.test()
async def test_picorv32_diffstep(dut):
    if int(os.environ.get("CUSTOM_IRQ", "0")) != 0:
        raise AssertionError("Diffstep test requires CUSTOM_IRQ=0")

    firmware_path = Path(
        os.environ.get("FIRMWARE", "firmware/firmware.hex")
    ).expanduser().resolve()
    spike_elf = Path(
        os.environ.get("SPIKE_ELF", str(firmware_path.with_suffix(".elf")))
    ).expanduser().resolve()
    if not spike_elf.is_file():
        raise AssertionError(f"Spike ELF does not exist: {spike_elf}")

    spike_command = [
        os.environ.get("SPIKE", "spike"),
        "-d",
        "--log-commits",
        f"--isa={os.environ.get('SPIKE_ISA', 'rv32imc')}",
        "--priv=m",
        f"--pc={os.environ.get('PROGRAM_BASE', '0x10000')}",
        "--disable-dtb",
        f"-m{os.environ.get('SPIKE_MEMORY', '0x10000:0x20000,0x10000000:0x1000,0x20000000:0x1000')}",
        str(spike_elf),
    ]
    spike_timeout = float(os.environ.get("SPIKE_STEP_TIMEOUT", "10"))
    max_cycles = int(os.environ.get("MAX_CYCLES", "1000000"))

    dut.clk.value = 1
    dut.resetn.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    memory_slave = AxiMemorySlave(dut, fw_path=firmware_path)
    cocotb.start_soon(memory_slave.run())

    spike = SpikeStepper(spike_command, spike_timeout)
    if spike.startup.strip():
        dut._log.info("Spike startup: %s", spike.startup.strip())

    retired = 0
    previous_order = None
    recent = deque(maxlen=12)
    try:
        await ClockCycles(dut.clk, 20)
        dut.resetn.value = 1

        for cycle in range(max_cycles):
            await RisingEdge(dut.clk)
            await ReadOnly()
            if not int(dut.rvfi_valid.value):
                continue

            order = int(dut.rvfi_order.value)
            insn = int(dut.rvfi_insn.value)
            rtl_trap = int(dut.rvfi_trap.value) != 0
            rtl_intr = int(dut.rvfi_intr.value) != 0

            if previous_order is None and order != 0:
                raise AssertionError(f"First RVFI order is {order}, expected 0")
            if previous_order is not None and order != previous_order + 1:
                raise AssertionError(
                    f"Non-consecutive RVFI order: previous={previous_order}, current={order}"
                )
            if (insn & 0x3) == 0x3 and (insn & 0x7F) == CUSTOM_0_OPCODE:
                raise AssertionError(
                    f"PicoRV32 custom-0 instruction retired at order={order}, "
                    f"pc=0x{int(dut.rvfi_pc_rdata.value):08x}, insn=0x{insn:08x}"
                )
            if rtl_intr:
                raise AssertionError(f"Unexpected interrupt retirement at RVFI order={order}")

            rtl_commit = sample_rtl_commit(dut)
            spike_step = spike.step(order)

            if (spike_step.pc, spike_step.insn) != (rtl_commit.pc, rtl_commit.insn):
                raise AssertionError(
                    mismatch_message(
                        "Spike and RTL executed different instructions",
                        recent,
                        rtl_commit,
                        spike_step,
                    )
                )

            if rtl_trap:
                if spike_step.commit is not None or not spike_step.exception:
                    raise AssertionError(
                        mismatch_message(
                            "RTL trapped but Spike did not report a non-retired exception",
                            recent,
                            rtl_commit,
                            spike_step,
                        )
                    )
                previous_order = order
                retired += 1
                break

            if spike_step.exception or spike_step.commit is None:
                raise AssertionError(
                    mismatch_message(
                        "RTL retired normally but Spike did not",
                        recent,
                        rtl_commit,
                        spike_step,
                    )
                )
            if rtl_commit != spike_step.commit:
                raise AssertionError(
                    mismatch_message(
                        "Spike and RTL commit effects differ",
                        recent,
                        rtl_commit,
                        spike_step,
                    )
                )

            recent.append(rtl_commit.line())
            previous_order = order
            retired += 1
            if retired % 10000 == 0:
                dut._log.info("Lockstep compared %d instructions", retired)
        else:
            raise AssertionError(f"Test did not finish within {max_cycles} cycles")
    finally:
        spike.close()

    if not memory_slave.passed:
        raise AssertionError("Core trapped before writing PASS_MAGIC")
    if retired == 0:
        raise AssertionError("Simulation completed without any RVFI retirements")

    dut._log.info(
        "Lockstep PASS in %d RTL cycles: %d RVFI records compared with Spike",
        cycle,
        retired,
    )
