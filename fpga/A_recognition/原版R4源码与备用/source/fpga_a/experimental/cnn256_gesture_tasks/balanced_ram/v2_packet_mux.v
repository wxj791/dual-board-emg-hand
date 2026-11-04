`default_nettype none
// Independent result and whole-sample mailboxes. Congestion never stalls DSP.
// Two byte-RAM slots replace the 832-bit pending and active shift registers.
// Producer contract: wave_data[831:128] stays stable for 104 clocks after
// wave_valid. fusion_features takes >7000 clocks per sample; only its header
// can change immediately, so the header is captured on the valid edge.
module v2_packet_mux (
 input wire clk,input wire rst_n,input wire flush,input wire [31:0] timestamp,
 input wire wave_valid,input wire [831:0] wave_data,
 input wire result_valid,input wire [255:0] result_data,
 output reg [31:0] wave_drops,output reg [31:0] result_drops,
 output wire frame_valid,input wire frame_ready,output reg [7:0] frame_type,
 output reg [10:0] frame_length,output reg [31:0] frame_timestamp,
 output wire payload_valid,input wire payload_ready,output wire [7:0] payload_data,
 input wire management_valid,output wire management_ready,input wire management_stop,
 input wire [10:0] management_length,input wire management_payload_valid,
 output wire management_payload_ready,input wire [7:0] management_data
);
 (* ram_style="bram" *) reg [7:0] packets[0:255];
 reg [7:0] packet_q;
 reg [127:0] capture_header;
 reg [255:0] result_hold;
 (* ram_style="bram" *) reg [7:0] result_packets[0:63];
 reg [7:0] result_q;reg result_capturing,result_capture_bank,result_pending_bank,result_active_bank;
 reg [4:0] result_capture_index,result_read_index;reg [1:0] result_slots;
 wire [4:0] result_prefetch=(state==2 && frame_type==8'h41 && payload_ready && left>1) ? result_read_index+1'b1 : result_read_index;
 always @(posedge clk) begin
  result_q<=result_packets[{result_active_bank,result_prefetch}];
  if(result_capturing && !flush) result_packets[{result_capture_bank,result_capture_index}]<=result_hold[result_capture_index*8 +:8];
 end
 reg [1:0] slots_busy;
 reg capturing,capture_bank,pending_bank,active_bank;
 reg [6:0] capture_index,read_index;
 reg [31:0] capture_time;
 reg [31:0] wave_time,result_time;
 reg wave_pending,result_pending;
 reg [1:0] state;
 reg [6:0] left;reg last_management;
 assign frame_valid=state==1;
 assign payload_valid=state==2 && (frame_type!=8'h42 || management_payload_valid);
 wire choose_management=management_valid && (management_stop || (!result_pending && (!wave_pending || !last_management))) && !flush;
 assign management_ready=state==0 && choose_management;
 assign management_payload_ready=state==2 && frame_type==8'h42 && payload_ready;
 wire [6:0] prefetch_index=(state==2 && payload_ready && left>1) ? read_index+1'b1 : read_index;
 wire [7:0] capture_byte=capture_index<16 ? capture_header[capture_index*8 +:8] : wave_data[capture_index*8 +:8];
 wire packet_write=!flush && (capturing || (wave_valid && slots_busy!=2'b11));
 wire [7:0] packet_write_address=capturing ? {capture_bank,capture_index} : {slots_busy[0],7'b0};
 wire [7:0] packet_write_data=capturing ? capture_byte : wave_data[7:0];
 assign payload_data=frame_type==8'h42 ? management_data : (frame_type==8'h40 ? packet_q : result_q);
 always @(posedge clk) begin
  packet_q<=packets[{active_bank,prefetch_index}];
  if(packet_write) packets[packet_write_address]<=packet_write_data;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   result_slots<=0;result_capturing<=0;result_capture_bank<=0;result_pending_bank<=0;result_active_bank<=0;result_capture_index<=0;result_read_index<=0;
   wave_pending<=0;result_pending<=0;state<=0;left<=0;last_management<=0;
   slots_busy<=0;capturing<=0;capture_bank<=0;pending_bank<=0;active_bank<=0;
   capture_index<=0;read_index<=0;capture_time<=0;
   frame_type<=0;frame_length<=0;frame_timestamp<=0;wave_drops<=0;result_drops<=0;
  end else begin
   if(flush) begin
    wave_pending<=0;result_pending<=0;capturing<=0;result_capturing<=0;
    result_slots<=(state!=0 && frame_type==8'h41) ? (result_active_bank ? 2'b10 : 2'b01) : 2'b00;
    slots_busy<=(state!=0 && frame_type==8'h40) ? (active_bank ? 2'b10 : 2'b01) : 2'b00;
   end
   if(capturing && !flush) begin
    if(capture_index==103) begin
     capturing<=0;wave_pending<=1;pending_bank<=capture_bank;wave_time<=capture_time;
    end else capture_index<=capture_index+1'b1;
   end
   if(wave_valid && !flush) begin
    if(capturing || wave_pending || slots_busy==2'b11) wave_drops<=wave_drops+1'b1;
    else begin
     capture_bank<=slots_busy[0];slots_busy[slots_busy[0]]<=1;
     capture_header<=wave_data[127:0];capture_time<=timestamp;
     capture_index<=1;capturing<=1;
    end
   end
   if(result_capturing && !flush) begin
    result_capture_index<=result_capture_index+1'b1;
    if(result_capture_index==31) begin result_capturing<=0;result_pending<=1;result_pending_bank<=result_capture_bank;end
   end
   if(result_valid) begin
    if((result_pending || result_capturing || result_slots==2'b11) && !flush) result_drops<=result_drops+1'b1;
    else begin
     result_hold<=result_data;result_time<=timestamp;result_capturing<=1;result_capture_index<=0;
     result_capture_bank<=flush ? (state!=0 && frame_type==8'h41 && !result_active_bank) : result_slots[0];
     if(flush) result_slots[(state!=0 && frame_type==8'h41 && !result_active_bank)]<=1;
     else result_slots[result_slots[0]]<=1;
    end
   end
   case(state)
    0:if(choose_management) begin
     last_management<=1;frame_type<=8'h42;frame_length<=management_length;left<=management_length[6:0];frame_timestamp<=timestamp;state<=1;
    end else if(result_pending && !flush) begin
     last_management<=0;result_active_bank<=result_pending_bank;result_read_index<=0;frame_type<=8'h41;frame_length<=32;
     left<=32;frame_timestamp<=result_time;result_pending<=0;state<=1;
    end else if(wave_pending && !flush) begin
     last_management<=0;active_bank<=pending_bank;read_index<=0;frame_type<=8'h40;frame_length<=104;
     left<=104;frame_timestamp<=wave_time;wave_pending<=0;state<=1;
    end
    1:if(frame_ready) state<=2;
    2:if(payload_ready && payload_valid) begin
     if(frame_type==8'h40) begin
      read_index<=read_index+1'b1;
      if(left==1) slots_busy[active_bank]<=0;
     end else if(frame_type==8'h41) begin result_read_index<=result_read_index+1'b1;if(left==1) result_slots[result_active_bank]<=0;end
     left<=left-1'b1;if(left==1) state<=0;
    end
   endcase
  end
 end
endmodule
`default_nettype wire
