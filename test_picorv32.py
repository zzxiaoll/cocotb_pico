import os
import sys
from enum import Enum, auto
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge, ReadWrite


RAM_BYTES = 128 * 1024
RAM_WORDS = RAM_BYTES // 4
RAM_BASE = 0x0001_0000

UART_ADDR = 0x1000_0000
PASS_ADDR = 0x2000_0000
PASS_MAGIC = 123456789
CUSTOM_0_OPCODE = 0x0B


def load_readmemh(path):
    path = Path(path).expanduser()
    memory = [0] * RAM_WORDS
    with open(path, "r", encoding = "ascii") as fw:
        for idx, line in enumerate(fw):
            if idx < RAM_WORDS:
                memory[idx] = int(line.strip(), 16)
    return memory


def is_ram_address(address):
    return RAM_BASE <= address < RAM_BASE + RAM_BYTES


def ram_word_index(address):
    return (address - RAM_BASE) // 4


def rvfi_commit_line(dut):
    """Return one deterministic, parser-friendly line for a retired instruction."""
    order = int(dut.rvfi_order.value)
    insn = int(dut.rvfi_insn.value)
    pc = int(dut.rvfi_pc_rdata.value)
    next_pc = int(dut.rvfi_pc_wdata.value)
    rs1_addr = int(dut.rvfi_rs1_addr.value)
    rs1_data = int(dut.rvfi_rs1_rdata.value)
    rs2_addr = int(dut.rvfi_rs2_addr.value)
    rs2_data = int(dut.rvfi_rs2_rdata.value)
    rd_addr = int(dut.rvfi_rd_addr.value)
    rd_data = int(dut.rvfi_rd_wdata.value)
    mem_addr = int(dut.rvfi_mem_addr.value)
    mem_rmask = int(dut.rvfi_mem_rmask.value)
    mem_wmask = int(dut.rvfi_mem_wmask.value)
    mem_rdata = int(dut.rvfi_mem_rdata.value)
    mem_wdata = int(dut.rvfi_mem_wdata.value)
    trap = int(dut.rvfi_trap.value)
    intr = int(dut.rvfi_intr.value)
    halt = int(dut.rvfi_halt.value)

    return (
        f"order={order:016d} "
        f"pc=0x{pc:08x} insn=0x{insn:08x} next=0x{next_pc:08x} "
        f"rs1=x{rs1_addr:02d}:0x{rs1_data:08x} "
        f"rs2=x{rs2_addr:02d}:0x{rs2_data:08x} "
        f"rd=x{rd_addr:02d}:0x{rd_data:08x} "
        f"mem=0x{mem_addr:08x} rmask=0x{mem_rmask:x} wmask=0x{mem_wmask:x} "
        f"rdata=0x{mem_rdata:08x} wdata=0x{mem_wdata:08x} "
        f"trap={trap} intr={intr} halt={halt}\n"
    )

class ReadState(Enum):
    IDLE = auto()
    RESP = auto()

class WriteState(Enum):
    COLLECT = auto()
    RESP = auto()

class AxiMemorySlave:
    def __init__(
        self,
        dut,
        fw_path
    ):
        self.dut = dut
        self.memory = load_readmemh(fw_path)
        self.read_state = ReadState.IDLE
        self.write_state = WriteState.COLLECT

        self.read_data = 0
        self.read_addr = 0

        self.write_addr = 0
        self.write_data = 0
        self.write_strb = 0
        self.latched_aw = False
        self.latched_w = False

        self.passed = False
        self.error = None

        self._reset()


    def _reset(self):
        self.dut.mem_axi_arready.value = 0
        self.dut.mem_axi_rvalid.value = 0
        self.dut.mem_axi_rdata.value = 0

        self.dut.mem_axi_awready.value = 0
        self.dut.mem_axi_wready.value = 0
        self.dut.mem_axi_bvalid.value = 0

        self.read_state = ReadState.IDLE
        self.write_state = WriteState.COLLECT
        self.read_data = 0
        self.read_addr = 0
        self.write_addr = 0
        self.write_data = 0
        self.write_strb = 0
        self.latched_aw = False
        self.latched_w = False
        self.passed = False
        self.error = None

    
    def _step_read_fsm(self):
        if self.read_state == ReadState.IDLE:
            ar_handshake = self.dut.mem_axi_arvalid.value and self.dut.mem_axi_arready.value
            if ar_handshake:
                self.read_addr = int(self.dut.mem_axi_araddr.value)
                if not is_ram_address(self.read_addr):
                    raise AssertionError(f"Read outside RAM: 0x{self.read_addr:08x}")
                self.read_data = self.memory[ram_word_index(self.read_addr)]
                self.read_state = ReadState.RESP

        elif self.read_state == ReadState.RESP:
            r_handshake = self.dut.mem_axi_rvalid.value and self.dut.mem_axi_rready.value
            if r_handshake:
                self.read_state = ReadState.IDLE


    def _step_write_fsm(self):
        if self.write_state == WriteState.COLLECT:
            aw_handshake = self.dut.mem_axi_awvalid.value and self.dut.mem_axi_awready.value
            w_handshake = self.dut.mem_axi_wvalid.value and self.dut.mem_axi_wready.value

            if aw_handshake:
                self.write_addr = int(self.dut.mem_axi_awaddr.value)
                self.latched_aw = True
            if w_handshake:
                self.write_data = int(self.dut.mem_axi_wdata.value)
                self.write_strb = int(self.dut.mem_axi_wstrb.value)
                self.latched_w = True
            
            if self.latched_aw and self.latched_w:
                # Write to memory
                if is_ram_address(self.write_addr):
                    word_addr = ram_word_index(self.write_addr)
                    for i in range(4):
                        if (self.write_strb >> i) & 1:
                            byte_shift = i * 8
                            byte_mask = 0xFF << byte_shift
                            self.memory[word_addr] = (self.memory[word_addr] & ~byte_mask) | ((self.write_data & byte_mask))
                
                # Check for UART write
                if self.write_addr == UART_ADDR:
                    uart_byte = self.write_data & 0xFF
                    sys.stdout.write(chr(uart_byte))
                    sys.stdout.flush()
                
                # Check for PASS write
                if self.write_addr == PASS_ADDR and self.write_data == PASS_MAGIC:
                    self.passed = True

                self.latched_aw = False
                self.latched_w = False
                self.write_state = WriteState.RESP

        elif self.write_state == WriteState.RESP:
            b_handshake = self.dut.mem_axi_bvalid.value and self.dut.mem_axi_bready.value
            if b_handshake:
                self.write_state = WriteState.COLLECT
                

    def _drive_outputs(self):
        # Drive read outputs
        if self.read_state == ReadState.IDLE:
            self.dut.mem_axi_arready.value = 1
            self.dut.mem_axi_rvalid.value = 0
        elif self.read_state == ReadState.RESP:
            self.dut.mem_axi_arready.value = 0
            self.dut.mem_axi_rvalid.value = 1
            self.dut.mem_axi_rdata.value = self.read_data

        # Drive write outputs
        if self.write_state == WriteState.COLLECT:
            if self.latched_aw:
                self.dut.mem_axi_awready.value = 0
            else:
                self.dut.mem_axi_awready.value = 1
            if self.latched_w:
                self.dut.mem_axi_wready.value = 0
            else:
                self.dut.mem_axi_wready.value = 1
            self.dut.mem_axi_bvalid.value = 0
        elif self.write_state == WriteState.RESP:
            self.dut.mem_axi_awready.value = 0
            self.dut.mem_axi_wready.value = 0
            self.dut.mem_axi_bvalid.value = 1

    
    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)

            if not self.dut.resetn.value:
                self._reset()
                continue
            
            await ReadWrite()
            self._drive_outputs()

            await ReadWrite()
            self._step_read_fsm()
            self._step_write_fsm()
            
            


@cocotb.test()
async def test_picorv32(dut):
    dut.clk.value = 1
    dut.resetn.value = 0
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())

    firmware_path = os.environ.get("FIRMWARE", "firmware/firmware.hex")
    rvfi_log_path = Path(os.environ.get("RVFI_LOG", "logs/rvfi_commit.log")).expanduser()
    max_cycles = int(os.environ.get("MAX_CYCLES", "1000000"))
    custom_irq_enabled = int(os.environ.get("CUSTOM_IRQ", "0")) != 0

    rvfi_log_path.parent.mkdir(parents=True, exist_ok=True)
    memory_slave = AxiMemorySlave(dut, fw_path=firmware_path)
    cocotb.start_soon(memory_slave.run())
    await ClockCycles(dut.clk, 20)
    dut.resetn.value = 1

    retired = 0
    previous_order = None
    with rvfi_log_path.open("w", encoding="ascii", buffering=1) as rvfi_log:
        for cycle in range(max_cycles):
            await RisingEdge(dut.clk)
            await ReadOnly()

            if int(dut.rvfi_valid.value):
                order = int(dut.rvfi_order.value)
                insn = int(dut.rvfi_insn.value)
                intr = int(dut.rvfi_intr.value)
                retired_trap = int(dut.rvfi_trap.value)

                if previous_order is None and order != 0:
                    raise AssertionError(f"First RVFI order is {order}, expected 0")
                if previous_order is not None and order != previous_order + 1:
                    raise AssertionError(
                        f"Non-consecutive RVFI order: previous={previous_order}, current={order}"
                    )
                if (
                    not custom_irq_enabled
                    and (insn & 0x3) == 0x3
                    and (insn & 0x7F) == CUSTOM_0_OPCODE
                ):
                    raise AssertionError(
                        f"PicoRV32 custom-0 instruction retired at order={order}, "
                        f"pc=0x{int(dut.rvfi_pc_rdata.value):08x}, insn=0x{insn:08x}"
                    )
                if intr and not custom_irq_enabled:
                    raise AssertionError(f"Unexpected interrupt retirement at RVFI order={order}")

                rvfi_log.write(rvfi_commit_line(dut))
                previous_order = order
                retired += 1

                if retired_trap:
                    break
        else:
            raise AssertionError(f"Test did not finish within {max_cycles} cycles")

    if not memory_slave.passed:
        raise AssertionError("Core trapped before writing PASS_MAGIC")
    if retired == 0:
        raise AssertionError("Simulation completed without any RVFI retirements")

    dut._log.info(
        "Test passed in %d cycles with %d retired instructions; RVFI log: %s",
        cycle,
        retired,
        rvfi_log_path,
    )
