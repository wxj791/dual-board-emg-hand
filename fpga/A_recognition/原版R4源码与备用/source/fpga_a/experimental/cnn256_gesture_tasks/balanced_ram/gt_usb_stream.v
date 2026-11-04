`default_nettype none
module gt_usb_stream(
 input wire clk,rst_n,flush,
 input wire raw_valid,input wire [223:0] raw_data,
 input wire result_valid,input wire [255:0] result_data,
 output wire frame_valid,input wire frame_ready,output wire [7:0] frame_type,
 output wire [10:0] frame_length,output wire payload_valid,input wire payload_ready,
 output wire [7:0] payload_data,output reg [15:0] drops,output wire [4:0] queued
);
 (* ram_style="bram" *) reg [7:0] memory[0:511];
 reg [15:0] result_slot;
 reg [3:0] write_slot,read_slot;
 reg [4:0] count,index,copy_index;
 reg active,copying,copy_result;
 reg [255:0] capture;
 reg [7:0] q;
 wire finish=active && payload_ready && index==(result_slot[read_slot] ? 31 : 27);
 wire publish=copying && copy_index==(copy_result ? 31 : 27) && !flush;
 assign frame_valid=count!=0 && !active && !flush;
 assign frame_type=result_slot[read_slot] ? 8'h41 : 8'h43;
 assign frame_length=result_slot[read_slot] ? 11'd32 : 11'd28;
 assign payload_valid=active;
 assign payload_data=q;assign queued=count;
 wire [4:0] prefetch=active && payload_ready && !finish ? index+1'b1 : index;
 always @(posedge clk) begin
  q<=memory[{read_slot,prefetch}];
  if(copying && !flush) memory[{write_slot,copy_index}]<=capture[7:0];
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   result_slot<=0;write_slot<=0;read_slot<=0;count<=0;index<=0;copy_index<=0;
   active<=0;copying<=0;copy_result<=0;capture<=0;drops<=0;
  end else begin
   if(flush) begin
    copying<=0;count<=active && !finish ? 1 : 0;
    write_slot<=active && !finish ? read_slot+1'b1 : (finish ? read_slot+1'b1 : read_slot);
   end else begin
    case({publish,finish})
     2'b10:count<=count+1'b1;
     2'b01:count<=count-1'b1;
    endcase
   end
   if(frame_valid && frame_ready) begin active<=1;index<=0;end
   if(active && payload_ready) begin
    index<=index+1'b1;
    if(finish) begin active<=0;index<=0;read_slot<=read_slot+1'b1;end
   end
   if(copying && !flush) begin
    capture<=capture>>8;copy_index<=copy_index+1'b1;
    if(publish) begin copying<=0;result_slot[write_slot]<=copy_result;write_slot<=write_slot+1'b1;end
   end
   if(result_valid || (raw_valid && !flush)) begin
    // Results win an exact-cycle collision. Count the omitted raw group too,
    // so a sequence gap can never be reported as a lossless capture.
    if((copying || count==16) && !flush) begin
     if(raw_valid && result_valid) drops<=drops>=65534 ? 16'hffff : drops+2'd2;
     else if(drops!=65535)drops<=drops+1'b1;
    end
    else begin
     if(raw_valid && result_valid && !flush && drops!=65535)drops<=drops+1'b1;
     capture<=result_valid ? result_data : {32'b0,raw_data};
     copy_result<=result_valid;copying<=1;copy_index<=0;
    end
   end
  end
 end
endmodule
`default_nettype wire
