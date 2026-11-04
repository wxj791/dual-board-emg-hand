`timescale 1ns/1ps
`default_nettype none
// Same glyphs as shared vga_font, with a synchronous row ROM for 148.5MHz.
module pcs_font_row(input wire clk,input wire [6:0] character,input wire [2:0] row,output reg [4:0] row_bits);
 (* ram_style="block" *) reg [4:0] rows[0:1023];
 initial $readmemh("fpga_b/gesture_tasks/assets/font_rows.hex",rows);
 always @(posedge clk)row_bits<=rows[{character,row}];
endmodule
`default_nettype wire
