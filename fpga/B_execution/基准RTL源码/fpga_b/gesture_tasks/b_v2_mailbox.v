`timescale 1ns/1ps
`default_nettype none
// Bundled-data CDC: hold is immutable from request until returned acknowledge.
// Destination publishes two pixel cycles after the frame boundary, in blanking.
// Local 32-bit reset/capture banks keep wide payload fanout off control paths.
module b_v2_mailbox #(parameter WIDTH=32)(
 input wire src_clk, src_rst_n, input wire src_valid,
 output wire src_ready, input wire [WIDTH-1:0] src_data,
 input wire dst_clk, dst_rst_n, dst_commit,
 output reg [WIDTH-1:0] dst_data, output reg dst_pulse);
 reg [WIDTH-1:0] payload_hold;
 reg req, ack;
 reg commit_q;
 always @(posedge dst_clk or negedge dst_rst_n)
  if(!dst_rst_n)commit_q<=0;else commit_q<=dst_commit;

 (* preserve="true", async_reg="true" *) reg ack_s1,ack_s2,req_s1,req_s2;
 assign src_ready=(req==ack_s2);
 always @(posedge src_clk or negedge src_rst_n)
  if(!src_rst_n) begin req<=0; payload_hold<=0; ack_s1<=0;ack_s2<=0; end
  else begin
   ack_s1<=ack; ack_s2<=ack_s1;
   if(src_valid && src_ready) begin payload_hold<=src_data; req<=~req; end
  end
 reg capture_pending;
 always @(posedge dst_clk or negedge dst_rst_n)
  if(!dst_rst_n) begin req_s1<=0;req_s2<=0;ack<=0;capture_pending<=0;dst_pulse<=0; end
  else begin
   req_s1<=req;req_s2<=req_s1;
   capture_pending<=commit_q && req_s2!=ack;dst_pulse<=capture_pending;
   if(commit_q && req_s2!=ack) begin
    ack<=req_s2;
   end
  end
 genvar bank;
 generate for(bank=0;bank<(WIDTH+31)/32;bank=bank+1)begin: payload_bank
  localparam BITS=(WIDTH-bank*32>=32)?32:WIDTH-bank*32;
  (* preserve="true" *) reg bank_reset_n,bank_capture;
  always @(posedge dst_clk or negedge dst_rst_n)
   if(!dst_rst_n)bank_reset_n<=0;else bank_reset_n<=1;
  always @(posedge dst_clk or negedge bank_reset_n)
   if(!bank_reset_n)begin bank_capture<=0;dst_data[bank*32+:BITS]<=0;end
   else begin
    bank_capture<=commit_q && req_s2!=ack;
    if(bank_capture)dst_data[bank*32+:BITS]<=payload_hold[bank*32+:BITS];
   end
 end endgenerate
endmodule
`default_nettype wire
