#!/usr/bin/env python3

import argparse
import re
from dataclasses import dataclass
from pathlib import Path


RTL_PATTERN = re.compile(
    r"order=(?P<order>\d+) "
    r"pc=0x(?P<pc>[0-9a-f]+) insn=0x(?P<insn>[0-9a-f]+) "
    r"next=0x(?P<next_pc>[0-9a-f]+) "
    r"rs1=x\d+:0x(?P<rs1_data>[0-9a-f]+) .*?"
    r"rd=x(?P<rd_addr>\d+):0x(?P<rd_data>[0-9a-f]+) "
    r"mem=0x(?P<mem_addr>[0-9a-f]+) "
    r"rmask=0x(?P<rmask>[0-9a-f]+) wmask=0x(?P<wmask>[0-9a-f]+) "
    r"rdata=0x(?P<rdata>[0-9a-f]+) wdata=0x(?P<wdata>[0-9a-f]+) "
    r"trap=(?P<trap>[01]) intr=(?P<intr>[01]) halt=(?P<halt>[01])"
)
SPIKE_PATTERN = re.compile(
    r"^core\s+\d+:\s+\d+\s+"
    r"(?P<pc>0x[0-9a-f]+)\s+\((?P<insn>0x[0-9a-f]+)\)"
    r"(?P<effects>.*)$"
)
SPIKE_RD_PATTERN = re.compile(r"\bx(?P<addr>\d+)\s+(?P<data>0x[0-9a-f]+)")
SPIKE_MEM_PATTERN = re.compile(
    r"\bmem\s+(?P<addr>0x[0-9a-f]+)(?:\s+(?P<data>0x[0-9a-f]+))?"
)


@dataclass(frozen=True)
class Commit:
    order: int
    pc: int
    insn: int
    rd_addr: int | None
    rd_data: int | None
    mem_kind: str | None
    mem_addr: int | None
    mem_data: int | None

    def line(self):
        rd = "-" if self.rd_addr is None else f"x{self.rd_addr:02d}:0x{self.rd_data:08x}"
        if self.mem_kind is None:
            memory = "-"
        elif self.mem_data is None:
            memory = f"{self.mem_kind}:0x{self.mem_addr:08x}"
        else:
            memory = f"{self.mem_kind}:0x{self.mem_addr:08x}:0x{self.mem_data:08x}"
        return (
            f"order={self.order:016d} pc=0x{self.pc:08x} insn=0x{self.insn:08x} "
            f"rd={rd} mem={memory}"
        )


def lane_offset(mask):
    if mask == 0:
        return 0
    return (mask & -mask).bit_length() - 1


def packed_store_data(wmask, wdata):
    result = 0
    output_byte = 0
    for lane in range(4):
        if (wmask >> lane) & 1:
            result |= ((wdata >> (8 * lane)) & 0xFF) << (8 * output_byte)
            output_byte += 1
    return result


def sign_extend(value, width):
    sign_bit = 1 << (width - 1)
    return (value & (sign_bit - 1)) - (value & sign_bit)


def rtl_load_address(insn, rs1_data, aligned_addr, rmask):
    if insn & 0x7F == 0x03:
        immediate = sign_extend((insn >> 20) & 0xFFF, 12)
        return (rs1_data + immediate) & 0xFFFFFFFF
    return aligned_addr + lane_offset(rmask)


def parse_rtl(path):
    commits = []
    trapped = 0
    for line_number, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        match = RTL_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"Malformed RTL log line {line_number}: {line}")

        fields = match.groupdict()
        values = {
            name: int(value, 16)
            for name, value in fields.items()
            if name not in {"order", "rd_addr", "trap", "intr", "halt"}
        }
        values.update(
            order=int(fields["order"]),
            rd_addr=int(fields["rd_addr"]),
            trap=int(fields["trap"]),
            intr=int(fields["intr"]),
            halt=int(fields["halt"]),
        )
        if values["trap"]:
            trapped += 1
            continue
        if values["intr"]:
            raise ValueError(f"Unexpected RTL interrupt at line {line_number}")

        rd_addr = values["rd_addr"] or None
        rd_data = values["rd_data"] if rd_addr is not None else None
        rmask = values["rmask"]
        wmask = values["wmask"]
        mem_kind = None
        mem_addr = None
        mem_data = None

        if rmask:
            mem_kind = "load"
            mem_addr = rtl_load_address(
                values["insn"], values["rs1_data"], values["mem_addr"], rmask
            )
        elif wmask:
            mem_kind = "store"
            mem_addr = values["mem_addr"] + lane_offset(wmask)
            mem_data = packed_store_data(wmask, values["wdata"])

        commits.append(
            Commit(
                order=len(commits),
                pc=values["pc"],
                insn=values["insn"],
                rd_addr=rd_addr,
                rd_data=rd_data,
                mem_kind=mem_kind,
                mem_addr=mem_addr,
                mem_data=mem_data,
            )
        )
    return commits, trapped


def parse_spike(path):
    commits = []
    for line in path.read_text(encoding="ascii").splitlines():
        match = SPIKE_PATTERN.match(line)
        if match is None:
            continue

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

        commits.append(
            Commit(
                order=len(commits),
                pc=int(match.group("pc"), 16),
                insn=int(match.group("insn"), 16),
                rd_addr=rd_addr,
                rd_data=rd_data,
                mem_kind=mem_kind,
                mem_addr=mem_addr,
                mem_data=mem_data,
            )
        )
    return commits


def compare(rtl_commits, spike_commits, max_mismatches=20):
    mismatches = []
    shared_count = min(len(rtl_commits), len(spike_commits))
    for index in range(shared_count):
        if rtl_commits[index] != spike_commits[index]:
            mismatches.append((index, rtl_commits[index], spike_commits[index]))
            if len(mismatches) >= max_mismatches:
                break
    return mismatches


def write_report(path, rtl_commits, spike_commits, trapped, mismatches):
    passed = not mismatches and len(rtl_commits) == len(spike_commits)
    lines = [
        f"result={'PASS' if passed else 'FAIL'}",
        f"rtl_nontrap_commits={len(rtl_commits)}",
        f"rtl_trap_records_ignored={trapped}",
        f"spike_commits={len(spike_commits)}",
        f"mismatches={len(mismatches)}",
    ]
    if len(rtl_commits) != len(spike_commits):
        lines.append(f"count_delta={len(rtl_commits) - len(spike_commits)}")
    for index, rtl_commit, spike_commit in mismatches:
        lines.extend(
            [
                "",
                f"mismatch_order={index}",
                f"rtl : {rtl_commit.line()}",
                f"spike: {spike_commit.line()}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return passed


def main():
    parser = argparse.ArgumentParser(description="Normalize Spike commits and compare them with PicoRV32 RVFI")
    parser.add_argument("--rtl", type=Path, required=True)
    parser.add_argument("--spike", type=Path, required=True)
    parser.add_argument("--spike-normalized", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    rtl_commits, trapped = parse_rtl(args.rtl)
    spike_commits = parse_spike(args.spike)
    args.spike_normalized.write_text(
        "".join(commit.line() + "\n" for commit in spike_commits), encoding="ascii"
    )
    mismatches = compare(rtl_commits, spike_commits)
    passed = write_report(args.report, rtl_commits, spike_commits, trapped, mismatches)
    print(args.report.read_text(encoding="ascii"), end="")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
