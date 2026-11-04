`timescale 1ns/1ps
module b_tasks_switch #(parameter integer STABLE_CLKS=480000)(
 input wire clk,rst_n,display_sw,initialized,output wire enabled
);
 (* async_reg="true",preserve="true" *)reg sw_meta,sw_sync;
 reg state;reg [19:0] count;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin sw_meta<=0;sw_sync<=0;state<=0;count<=0;end
  else begin
   // Switch ON closes to ground on the v1.0 schematic; fail closed at reset.
   sw_meta<=!display_sw;sw_sync<=sw_meta;
   if(sw_sync==state)count<=0;
   else if(count==STABLE_CLKS-1)begin state<=sw_sync;count<=0;end
   else count<=count+1'b1;
  end
 end
 assign enabled=state && initialized;
endmodule
