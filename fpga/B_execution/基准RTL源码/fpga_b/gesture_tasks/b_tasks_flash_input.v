`timescale 1ns/1ps
`default_nettype none
// SPI mode-0 data is stable during the low half-period before its rising
// sample edge. Capture halfway between the 24MHz controller edges, then
// consume it on the next rising edge; the SPI bit/byte schedule is unchanged.
module b_tasks_flash_input(input wire clk,miso,output reg captured);
 always @(negedge clk)captured<=miso;
endmodule
`default_nettype wire
