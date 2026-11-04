`timescale 1ns/1ps
`default_nettype none
// RAW stays in ADC mV in history: the renderer subtracts the visible mean.
// Other pages are normalized to native trace units (full lane = +/-22966).
module b_tasks_scale(
 input wire clk,rst_n,sample_valid,segment_break,
 input wire [127:0] raw_mv,input wire [191:0] filtered_q8,fast_q4,slow_q4,
 input wire [2:0] page,input wire [127:0] ranges,gains,input wire [7:0] custom,
 input wire [15:0] revision,input wire fifo_full,
 output reg valid,output reg [160:0] data,output reg saturated,
 output reg [31:0] dropped,output reg [15:0] busy_drops,full_drops
);
 reg [191:0] values;reg [127:0] range_hold,gain_hold,scaled;
 reg [7:0] custom_hold;reg [2:0] page_hold,lane;reg [15:0] rev_hold;
 reg working,emit_pending,clear_pending,sat;
 reg [2:0] phase;reg [15:0] range_lane,gain_lane;reg custom_lane;
 reg signed [24:0] value;reg [23:0] factor;
 reg signed [49:0] product;reg signed [34:0] quantized,normalized;
 // Rounded Q12 reciprocals: error < 0.5 native unit at every standard bound.
 function [23:0] coefficient;input [15:0] r;
  begin case(r)
   20:coefficient=4703437;50:coefficient=1881375;100:coefficient=940687;
   200:coefficient=470344;500:coefficient=188137;1200:coefficient=78391;
   default:coefficient=28506;
  endcase end
 endfunction
 integer k;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin values<=0;range_hold<=0;gain_hold<=0;scaled<=0;custom_hold<=0;page_hold<=0;
   lane<=0;rev_hold<=0;working<=0;emit_pending<=0;clear_pending<=1;valid<=0;data<=0;
   saturated<=0;dropped<=0;busy_drops<=0;full_drops<=0;sat<=0;
   phase<=0;range_lane<=0;gain_lane<=0;custom_lane<=0;value<=0;factor<=0;product<=0;quantized<=0;normalized<=0;end
  else begin
   valid<=0;
   if(segment_break)clear_pending<=1;
   if(sample_valid && page<4)begin
    if(working || emit_pending)begin dropped<=dropped+1'b1;busy_drops<=busy_drops+1'b1;clear_pending<=1;end
    else begin
     for(k=0;k<8;k=k+1)case(page)
      0:values[k*24+:24]<={8'd0,raw_mv[k*16+:16]};
      1:values[k*24+:24]<=filtered_q8[k*24+:24];
      2:values[k*24+:24]<=fast_q4[k*24+:24];
      3:values[k*24+:24]<=slow_q4[k*24+:24];
     endcase
     range_hold<=ranges;gain_hold<=gains;custom_hold<=custom;page_hold<=page;rev_hold<=revision;
     lane<=0;phase<=0;working<=1;sat<=0;
    end
   end
   // One channel uses six short cycles, under 3us for all eight at 24MHz.
   // Preserve exact rounding and clipping; scaling never changes input data.
   if(working)case(phase)
    0:begin
     value<=page_hold==1?$signed({values[lane*24+23],values[lane*24+:24]}):$signed({1'b0,values[lane*24+:24]});
     range_lane<=range_hold[lane*16+:16];gain_lane<=gain_hold[lane*16+:16];custom_lane<=custom_hold[lane];phase<=1;
    end
    1:begin factor<=custom_lane?{8'd0,gain_lane}:coefficient(range_lane);phase<=2;end
    2:begin product<=value*$signed({1'b0,factor});phase<=3;end
    3:begin
     if(custom_lane)quantized<=page_hold==1?(product+2048)>>>12:(product+128)>>>8;
     else quantized<=page_hold==1?(product+524288)>>>20:(product+32768)>>>16;
     phase<=4;
    end
    4:begin normalized<=page_hold>=2?(quantized<<<1)-22966:quantized;phase<=5;end
    5:begin
     if(page_hold==0)scaled[lane*16+:16]<=value[15:0];
     else begin
      scaled[lane*16+:16]<=normalized>22966?16'sd22966:normalized< -22966?-16'sd22966:normalized[15:0];
      if(normalized>22966 || normalized< -22966)sat<=1;
     end
     phase<=0;
     if(lane==7)begin working<=0;emit_pending<=1;end else lane<=lane+1'b1;
    end
   endcase
   if(emit_pending)begin
    emit_pending<=0;
    if(fifo_full)begin dropped<=dropped+1'b1;full_drops<=full_drops+1'b1;clear_pending<=1;end
    else begin data<={16'd0,rev_hold,clear_pending||segment_break,scaled};valid<=1;clear_pending<=0;saturated<=sat;end
   end
  end
 end
endmodule
`default_nettype wire
