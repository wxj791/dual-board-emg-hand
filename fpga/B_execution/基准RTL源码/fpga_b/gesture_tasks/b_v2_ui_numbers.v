`timescale 1ns/1ps
`default_nettype none
// One digit converter, two values. Runs for 32 clocks during vertical blanking.
module b_v2_ui_numbers(input wire clk,rst_n,update,input wire [15:0] rate,
 input wire [9:0] fill,output reg [19:0] rate_bcd,output reg [11:0] fill_bcd);
 reg busy,phase;reg [4:0] step;reg [15:0] binary,fill_hold;reg [19:0] digits;
 reg [19:0] adjusted;integer i;
 always @*begin
  adjusted=digits;
  for(i=0;i<5;i=i+1)if(digits[i*4+:4]>=5)adjusted[i*4+:4]=digits[i*4+:4]+3;
 end
 wire [19:0] shifted={adjusted[18:0],binary[15]};
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin busy<=0;phase<=0;step<=0;binary<=0;digits<=0;fill_hold<=0;rate_bcd<=0;fill_bcd<=0;end
  else if(update && !busy)begin busy<=1;phase<=0;step<=0;binary<=rate;fill_hold<={6'd0,fill};digits<=0;end
  else if(busy)begin
   binary<={binary[14:0],1'b0};digits<=shifted;step<=step+1'b1;
   if(step==15)begin
    if(!phase)begin rate_bcd<=shifted;phase<=1;step<=0;binary<=fill_hold;digits<=0;end
    else begin fill_bcd<=shifted[11:0];busy<=0;end
   end
  end
endmodule
`default_nettype wire
