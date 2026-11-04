`timescale 1ns/1ps
`default_nettype none
// GIF timestamps rounded cumulatively to video frames; update only at vblank.
module b_v2_whale_player(input wire clk,rst_n,frame_start,output reg [4:0] frame);
 function [4:0] hold_ticks;input [4:0] n;
  begin case(n)
   5'd0:hold_ticks=5'd7;
   5'd1:hold_ticks=5'd6;
   5'd2:hold_ticks=5'd7;
   5'd3:hold_ticks=5'd6;
   5'd4:hold_ticks=5'd7;
   5'd5:hold_ticks=5'd7;
   5'd6:hold_ticks=5'd7;
   5'd7:hold_ticks=5'd8;
   5'd8:hold_ticks=5'd8;
   5'd9:hold_ticks=5'd10;
   5'd10:hold_ticks=5'd10;
   5'd11:hold_ticks=5'd14;
   5'd12:hold_ticks=5'd19;
   5'd13:hold_ticks=5'd25;
   5'd14:hold_ticks=5'd19;
   5'd15:hold_ticks=5'd10;
   5'd16:hold_ticks=5'd9;
   5'd17:hold_ticks=5'd8;
   5'd18:hold_ticks=5'd7;
   5'd19:hold_ticks=5'd8;
   5'd20:hold_ticks=5'd7;
   5'd21:hold_ticks=5'd7;
   5'd22:hold_ticks=5'd7;
   5'd23:hold_ticks=5'd8;
   default:hold_ticks=5'd7;
  endcase end
 endfunction
 reg [4:0] remaining;
 wire [4:0] next_frame=frame==23?5'd0:frame+1'b1;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin frame<=0;remaining<=5'd7;end
  else if(frame_start)begin
   if(remaining==1)begin frame<=next_frame;remaining<=hold_ticks(next_frame);end
   else remaining<=remaining-1'b1;
  end
endmodule
`default_nettype wire
