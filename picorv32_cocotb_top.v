// This is free and unencumbered software released into the public domain.
//
// Anyone is free to copy, modify, publish, use, compile, sell, or
// distribute this software, either in source code form or as a compiled
// binary, for any purpose, commercial or non-commercial, and by any
// means.

`timescale 1 ns / 1 ps

module picorv32_cocotb_top #(
	parameter AXI_TEST = 0,
	parameter VERBOSE = 0
) (
    input  wire        clk,
    input  wire        resetn,

    output wire        trap,

    output wire        mem_axi_awvalid,
    input  wire        mem_axi_awready,
    output wire [31:0] mem_axi_awaddr,
    output wire [ 2:0] mem_axi_awprot,

    output wire        mem_axi_wvalid,
    input  wire        mem_axi_wready,
    output wire [31:0] mem_axi_wdata,
    output wire [ 3:0] mem_axi_wstrb,

    input  wire        mem_axi_bvalid,
    output wire        mem_axi_bready,

    output wire        mem_axi_arvalid,
    input  wire        mem_axi_arready,
    output wire [31:0] mem_axi_araddr,
    output wire [ 2:0] mem_axi_arprot,

    input  wire        mem_axi_rvalid,
    output wire        mem_axi_rready,
    input  wire [31:0] mem_axi_rdata,

	output wire        rvfi_valid,
	output wire [63:0] rvfi_order,
	output wire [31:0] rvfi_insn,
	output wire        rvfi_trap,
	output wire        rvfi_halt,
	output wire        rvfi_intr,
	output wire [ 4:0] rvfi_rs1_addr,
	output wire [ 4:0] rvfi_rs2_addr,
	output wire [31:0] rvfi_rs1_rdata,
	output wire [31:0] rvfi_rs2_rdata,
	output wire [ 4:0] rvfi_rd_addr,
	output wire [31:0] rvfi_rd_wdata,
	output wire [31:0] rvfi_pc_rdata,
	output wire [31:0] rvfi_pc_wdata,
	output wire [31:0] rvfi_mem_addr,
	output wire [ 3:0] rvfi_mem_rmask,
	output wire [ 3:0] rvfi_mem_wmask,
	output wire [31:0] rvfi_mem_rdata,
	output wire [31:0] rvfi_mem_wdata,

    output wire        trace_valid,
    output wire [35:0] trace_data
);

	`ifdef DISABLE_CUSTOM_IRQ
	localparam CUSTOM_IRQ_ENABLE = 0;
	`else
	localparam CUSTOM_IRQ_ENABLE = 1;
	`endif

	reg [15:0] count_cycle = 0;
	reg [31:0] irq = 0;
	always @(posedge clk) count_cycle <= resetn ? count_cycle + 1 : 0;

	always @* begin
		irq = 0;
	`ifndef DISABLE_CUSTOM_IRQ
		irq[4] = &count_cycle[12:0];
		irq[5] = &count_cycle[15:0];
	`endif
	end

	picorv32_axi #(
`ifndef SYNTH_TEST
`ifdef SP_TEST
		.ENABLE_REGS_DUALPORT(0),
`endif
`ifdef COMPRESSED_ISA
		.COMPRESSED_ISA(1),
`endif
		.ENABLE_MUL(1),
		.ENABLE_DIV(1),
		.ENABLE_IRQ(CUSTOM_IRQ_ENABLE),
		.ENABLE_IRQ_TIMER(CUSTOM_IRQ_ENABLE),
		.PROGADDR_RESET(32'h0001_0000),
		.ENABLE_TRACE(1)
`endif
	) uut (
		.clk            (clk            ),
		.resetn         (resetn         ),
		.trap           (trap           ),
		.mem_axi_awvalid(mem_axi_awvalid),
		.mem_axi_awready(mem_axi_awready),
		.mem_axi_awaddr (mem_axi_awaddr ),
		.mem_axi_awprot (mem_axi_awprot ),
		.mem_axi_wvalid (mem_axi_wvalid ),
		.mem_axi_wready (mem_axi_wready ),
		.mem_axi_wdata  (mem_axi_wdata  ),
		.mem_axi_wstrb  (mem_axi_wstrb  ),
		.mem_axi_bvalid (mem_axi_bvalid ),
		.mem_axi_bready (mem_axi_bready ),
		.mem_axi_arvalid(mem_axi_arvalid),
		.mem_axi_arready(mem_axi_arready),
		.mem_axi_araddr (mem_axi_araddr ),
		.mem_axi_arprot (mem_axi_arprot ),
		.mem_axi_rvalid (mem_axi_rvalid ),
		.mem_axi_rready (mem_axi_rready ),
		.mem_axi_rdata  (mem_axi_rdata  ),
		.pcpi_valid     (               ),
		.pcpi_insn      (               ),
		.pcpi_rs1       (               ),
		.pcpi_rs2       (               ),
		.pcpi_wr        (1'b0           ),
		.pcpi_rd        (32'b0          ),
		.pcpi_wait      (1'b0           ),
		.pcpi_ready     (1'b0           ),
		.irq            (irq            ),
		.eoi            (               ),
		.rvfi_valid     (rvfi_valid     ),
		.rvfi_order     (rvfi_order     ),
		.rvfi_insn      (rvfi_insn      ),
		.rvfi_trap      (rvfi_trap      ),
		.rvfi_halt      (rvfi_halt      ),
		.rvfi_intr      (rvfi_intr      ),
		.rvfi_rs1_addr  (rvfi_rs1_addr  ),
		.rvfi_rs2_addr  (rvfi_rs2_addr  ),
		.rvfi_rs1_rdata (rvfi_rs1_rdata ),
		.rvfi_rs2_rdata (rvfi_rs2_rdata ),
		.rvfi_rd_addr   (rvfi_rd_addr   ),
		.rvfi_rd_wdata  (rvfi_rd_wdata  ),
		.rvfi_pc_rdata  (rvfi_pc_rdata  ),
		.rvfi_pc_wdata  (rvfi_pc_wdata  ),
		.rvfi_mem_addr  (rvfi_mem_addr  ),
		.rvfi_mem_rmask (rvfi_mem_rmask ),
		.rvfi_mem_wmask (rvfi_mem_wmask ),
		.rvfi_mem_rdata (rvfi_mem_rdata ),
		.rvfi_mem_wdata (rvfi_mem_wdata ),
		.trace_valid    (trace_valid    ),
		.trace_data     (trace_data     )
	);

endmodule
