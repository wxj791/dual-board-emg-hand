`timescale 1ns/1ps
`default_nettype none
// EG4 PLL: EXPERIMENT: 24 MHz / 2 * 99 / 8 = 148.5 MHz, VCO = 1188 MHz.
// VCO feedback avoids a low phase-detector frequency. Analog parameters are
// from the official 972 MHz VCO example; physical lock remains board testing.
module vga_pll_25m2(input wire refclk,reset,output wire clk_pix,clk_dac,locked);
 wire [4:0] clocks;
 EG_LOGIC_BUFG pixel_buf(.i(clocks[0]),.o(clk_pix));
 EG_LOGIC_BUFG dac_buf(.i(clocks[1]),.o(clk_dac));
 EG_PHY_PLL #(.FIN("24.000"),.REFCLK_DIV(2),.FBCLK_DIV(99),
  .CLKC1_DIV(8),.CLKC1_ENABLE("ENABLE"),.CLKC1_CPHASE(2),.CLKC1_FPHASE(0),
  .CLKC0_DIV(8),.CLKC0_ENABLE("ENABLE"),.CLKC0_CPHASE(7),.CLKC0_FPHASE(0),
  .FEEDBK_MODE("NORMAL"),.FEEDBK_PATH("VCO_PHASE_0"),
  .STDBY_ENABLE("DISABLE"),.PLLRST_ENA("ENABLE"),.SYNC_ENABLE("DISABLE"),
  .GMC_GAIN(0),.ICP_CURRENT(9),.KVCO(2),.LPF_CAPACITOR(2),.LPF_RESISTOR(8),
  .DERIVE_PLL_CLOCKS("DISABLE"),.GEN_BASIC_CLOCK("DISABLE")) pll(
  .refclk(refclk),.reset(reset),.stdby(1'b0),.extlock(locked),
  // VCO_PHASE_0 selects internal VCO feedback; external fbclk is unused.
  .fbclk(1'b0),.clkc(clocks),.load_reg(1'b0),.psclk(1'b0),.psdown(1'b0),
  .psstep(1'b0),.psclksel(3'b0),.psdone(),.dclk(1'b0),.dcs(1'b0),
  .dwe(1'b0),.di(8'b0),.daddr(6'b0),.\do ());
endmodule
`default_nettype wire
