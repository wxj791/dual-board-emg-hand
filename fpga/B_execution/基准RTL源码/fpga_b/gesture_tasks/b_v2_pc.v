`timescale 1ns/1ps
`default_nettype none
// Binary request collector and backpressured framed response transmitter.
module b_v2_pc(
 input wire clk,rst_n,frame_valid,output wire frame_ready,input wire [7:0] frame_type,
 input wire [10:0] frame_length,input wire payload_valid,output wire payload_ready,
 input wire [7:0] payload_data,input wire payload_last,
 output reg command_valid,input wire command_ready,
 output reg [15:0] request_id,opcode,data_length,output reg [79:0] arguments,
 input wire response_valid,output wire response_ready,input wire [7:0] response_type,
 input wire [15:0] response_id,response_opcode,response_status,
 input wire [9:0] response_length,input wire [4095:0] response_data,input wire [7:0] response_byte,output wire [9:0] response_byte_index,
 output wire uart_tx,output reg [31:0] rejects
);
 reg collecting;reg [10:0] index,length;reg [7:0] kind;reg [143:0] packet;
 wire [143:0] complete={payload_data,packet[143:8]};
 // Assemble indexed bytes, so malformed long packets cannot shift a valid suffix into a request.
 reg [143:0] fixed_packet;
 reg evaluate;
 assign frame_ready=!collecting && !command_valid && !evaluate;
 assign payload_ready=collecting;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin collecting<=0;index<=0;length<=0;kind<=0;packet<=0;fixed_packet<=0;
   command_valid<=0;request_id<=0;opcode<=0;data_length<=0;arguments<=0;rejects<=0;evaluate<=0;end
  else begin
   if(command_valid && command_ready)command_valid<=0;
   if(frame_valid && frame_ready)begin
    collecting<=frame_length!=0;index<=0;length<=frame_length;kind<=frame_type;fixed_packet<=0;
    if(frame_length==0)rejects<=rejects+1'b1;
   end
   if(payload_valid && payload_ready)begin
    if(index<18)fixed_packet[index*8+:8]<=payload_data;
    index<=index+1'b1;
    if(payload_last)begin collecting<=0;evaluate<=1;end
   end
   if(evaluate)begin
    evaluate<=0;
    if((kind==8'h50 || kind==8'h42) && length>=8 && length<=18 && index==length && fixed_packet[7:0]==1 &&
     fixed_packet[15:8]==2 && fixed_packet[63:48]==length-8)begin
     request_id<=fixed_packet[31:16];opcode<=fixed_packet[47:32];data_length<=fixed_packet[63:48];
     arguments<=fixed_packet[143:64];command_valid<=1;
    end else rejects<=rejects+1'b1;
   end
  end
 end
 reg sending,buffering,copy_phase;
 assign response_byte_index=copy_index;reg [9:0] copy_index;reg [10:0] tx_index,total;reg [15:0] tx_sequence,crc;
 reg [7:0] kind_hold;reg [15:0] id_hold,op_hold,status_hold;reg [9:0] len_hold;
 (* ram_style="block" *) reg [7:0] data_buffer[0:511];reg [7:0] buffer_byte;
 // The response producer holds its snapshot until the byte copy completes.
 // UART transmission then owns a compact BRAM, freeing PC/control for STOP.
 always @(posedge clk)begin
  if(buffering && copy_phase && copy_index<len_hold)data_buffer[copy_index[8:0]]<=
   response_byte;
  buffer_byte<=data_buffer[tx_index[8:0]-9'd22];
 end
 reg tx_start;wire tx_busy;reg [7:0] tx_data;reg [7:0] next_byte;
 wire [15:0] crc_next;
 link_crc16_ccitt crc_step(.crc_in(crc),.data_in(next_byte),.crc_out(crc_next));
 wire [10:0] outer_len={1'b0,len_hold}+11'd10;
 assign response_ready=buffering && copy_index==len_hold;
 always @*begin
  next_byte=0;
  case(tx_index)
   0:next_byte=8'ha5;1:next_byte=8'h5a;2:next_byte=1;3:next_byte=kind_hold;
   4:next_byte=tx_sequence[7:0];5:next_byte=tx_sequence[15:8];
   6:next_byte=outer_len[7:0];7:next_byte={5'd0,outer_len[10:8]};
   12:next_byte=1;13:next_byte=2;14:next_byte=id_hold[7:0];15:next_byte=id_hold[15:8];
   16:next_byte=op_hold[7:0];17:next_byte=op_hold[15:8];
   18:next_byte=status_hold[7:0];19:next_byte=status_hold[15:8];
   20:next_byte=len_hold[7:0];21:next_byte={6'd0,len_hold[9:8]};
   default:begin
    if(tx_index>=22 && tx_index<22+len_hold)next_byte=buffer_byte;
    if(tx_index==total-2)next_byte=crc[7:0];
    if(tx_index==total-1)next_byte=crc[15:8];
   end
  endcase
 end
 uart_tx_byte #(.CLKS_PER_BIT(26)) tx(.clk(clk),.rst_n(rst_n),.start(tx_start),.data(tx_data),.tx(uart_tx),.busy(tx_busy));
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin sending<=0;buffering<=0;copy_phase<=0;copy_index<=0;tx_index<=0;total<=0;tx_sequence<=0;crc<=16'hffff;kind_hold<=0;
   id_hold<=0;op_hold<=0;status_hold<=0;len_hold<=0;tx_start<=0;tx_data<=0;end
  else begin
   tx_start<=0;
   if(response_valid && !sending && !buffering && !tx_busy && !tx_start)begin
    buffering<=1;copy_phase<=0;copy_index<=0;tx_index<=0;total<=24+response_length;crc<=16'hffff;
    kind_hold<=response_type;id_hold<=response_id;op_hold<=response_opcode;status_hold<=response_status;
    len_hold<=response_length;
   end
   if(buffering)begin
    copy_phase<=!copy_phase;
    if(copy_index==len_hold)begin buffering<=0;sending<=1;end
    else if(copy_phase)copy_index<=copy_index+1'b1;
   end
   if(sending && !tx_busy && !tx_start)begin
    tx_data<=next_byte;tx_start<=1;
    if(tx_index>=2 && tx_index<total-2)crc<=crc_next;
    if(tx_index==total-1)begin sending<=0;tx_sequence<=tx_sequence+1'b1;end
    else tx_index<=tx_index+1'b1;
   end
  end
 end
endmodule
`default_nettype wire
