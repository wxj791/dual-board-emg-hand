`timescale 1ns/1ps
`default_nettype none
// Native 1920x1080@60 timing, 148.5MHz. Existing 640x480 canvas is
// displayed 3x horizontally and 9/4 vertically, filling all active scan lines.
// This first experiment establishes clock/timing feasibility, not new artwork detail.
module vga_timing(input wire clk,rst_n,
 output reg [11:0] h,v,output wire hs,vs,de,
 output wire [9:0] x,output wire [8:0] y,output reg frame_start,
 output reg [8:0] wave_x,output reg [4:0] wave_phase);
 reg wave_window;
 reg x_window;
 reg [1:0] x_repeat;reg [9:0] logical_x;
 reg [3:0] y_phase;reg [8:0] logical_y;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin x_window<=0;wave_window<=0;frame_start<=1;h<=0;v<=0;x_repeat<=0;logical_x<=0;y_phase<=0;logical_y<=0;wave_phase<=0;wave_x<=0;end
  else begin
   x_window<=h>=191 && h<2110;
   wave_window<=h>=383 && h<2110;
   frame_start<=h==2199 && v==1124;
   if(h==2199)begin
    h<=0;v<=v==1124?0:v+1'b1;
    if(v<41 || v>=1120)begin logical_y<=0;y_phase<=0;end
    else if(y_phase>=5)begin y_phase<=y_phase-5;logical_y<=logical_y+1'b1;end
    else y_phase<=y_phase+4;
   end
   else h<=h+1'b1;
   if(!x_window)begin x_repeat<=0;logical_x<=0;end
   else if(x_repeat==2)begin x_repeat<=0;logical_x<=logical_x+1'b1;end
   else x_repeat<=x_repeat+1'b1;
   // 512 history points over 1728 physical pixels: floor((h-384)*8/27).
   if(!wave_window)begin wave_phase<=0;wave_x<=0;end
   else if(wave_phase>=19)begin wave_phase<=wave_phase-19;wave_x<=wave_x+1'b1;end
   else wave_phase<=wave_phase+8;
  end
 assign hs=(h<44);assign vs=(v<5); // positive H/V sync for this mode
 assign de=(h>=192 && h<2112 && v>=41 && v<1121);
 assign x=logical_x;assign y=logical_y;

endmodule
`default_nettype wire
