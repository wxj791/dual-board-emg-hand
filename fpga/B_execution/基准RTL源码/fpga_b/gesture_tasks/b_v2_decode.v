`timescale 1ns/1ps
`default_nettype none
// Input has already passed the shared outer CRC parser. Commit whole payloads only.
module b_v2_decode #(parameter integer STALE_CLKS=7200000)(
 input wire clk,rst_n,frame_valid,output wire frame_ready,
 input wire [7:0] frame_type,input wire [10:0] frame_length,
 input wire payload_valid,output wire payload_ready,input wire [7:0] payload_data,input wire payload_last,
 output reg wave_valid,result_valid,segment_break,output reg [127:0] raw_mv,
 output reg [191:0] filtered_q8,fast_q4,slow_q4,
 output reg [31:0] sample_seq,window_seq,cnn_cycles,head_cycles,
 output reg [15:0] sample_rate,generation,epoch,class_id,class_count,action_hint,margin,
 output reg [7:0] wave_flags,result_flags,
 output wire stale,output reg [31:0] accepted,rejected,breaks,
 output reg result_fault,output reg [31:0] reject_info,output reg [255:0] rejected_payload
);
 reg collecting,finalize;reg [7:0] kind;reg [10:0] length,index;
 reg [831:0] bytes;
 // Payload storage has no reset value: a complete 104/32-byte transaction
 // overwrites every bit used by its decoder before finalize can commit it.
 always @(posedge clk)if(payload_valid && payload_ready)bytes<={payload_data,bytes[831:8]};
 wire [831:0] complete=bytes;
 // Short result payload ends in the high 256 bits of the shift register.
 wire [255:0] r=complete[831:576];
 reg wave_seen,result_seen;reg [31:0] age;reg [15:0] wave_gen,wave_epoch,result_gen,result_epoch;
 wire wave_ok=complete[7:0]==1 && complete[15:8]==15 && complete[23:16]==8 &&
  complete[31:27]==0 && complete[127:112]==0 && complete[79:64]!=0;
 wire result_ok=r[7:0]==1 && r[15:10]==0 && r[159:144]==0 && r[255:224]==0 &&
  r[63:48]<=256 && r[127:112]!=0 && (r[31:16]==16'hffff || r[31:16]<r[127:112]);
 wire [7:0] reject_reasons={
  (r[127:112]==0 || (r[31:16]!=16'hffff && r[31:16]>=r[127:112])),
  r[63:48]>256,(r[159:144]!=0 || r[255:224]!=0),(r[7:0]!=1 || r[15:10]!=0),
  !lanes_ok,!wave_ok,(kind!=8'h40 && kind!=8'h41 && kind!=8'h11),
  (index!=length || (kind==8'h40 && length!=104) || (kind==8'h41 && length!=32))};
 wire rejecting=finalize && (index!=length ||
  (kind==8'h40 ? !(length==104 && wave_ok && lanes_ok) :
   kind==8'h41 ? !(length==32 && result_ok) : kind!=8'h11));
 reg lanes_ok;integer k;
 always @*begin
  lanes_ok=1;
  for(k=0;k<8;k=k+1)begin
   if(complete[128+k*16+:16]>3300)lanes_ok=0;
   if(complete[448+k*24+:24]>24'h0fffff || complete[640+k*24+:24]>24'h0fffff)lanes_ok=0;
  end
 end
 assign frame_ready=!collecting;assign payload_ready=collecting && !finalize;
 assign stale=age>=STALE_CLKS;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   collecting<=0;finalize<=0;kind<=0;length<=0;index<=0;
   wave_valid<=0;result_valid<=0;segment_break<=0;raw_mv<=0;filtered_q8<=0;fast_q4<=0;slow_q4<=0;
   sample_seq<=0;window_seq<=0;cnn_cycles<=0;head_cycles<=0;sample_rate<=0;generation<=0;epoch<=0;
   class_id<=16'hffff;class_count<=0;action_hint<=16'hffff;margin<=0;wave_flags<=0;result_flags<=0;
   wave_seen<=0;result_seen<=0;wave_gen<=0;wave_epoch<=0;result_gen<=0;result_epoch<=0;
   age<=STALE_CLKS;accepted<=0;rejected<=0;breaks<=0;
   result_fault<=0;reject_info<=0;rejected_payload<=0;
  end else begin
   wave_valid<=0;result_valid<=0;segment_break<=0;result_fault<=0;
   if(rejecting)begin
    reject_info<={kind,reject_reasons,5'd0,length};rejected_payload<=r;
    if(kind==8'h41)begin
     // A CRC-valid but malformed classification invalidates motion authority.
     // It does not invalidate the independently sequenced waveform stream.
     result_fault<=1;result_seen<=0;result_flags<=0;class_id<=16'hffff;
    end else begin segment_break<=1;breaks<=breaks+1'b1;end
   end
   if(!stale)age<=age+1'b1;
   if(age==STALE_CLKS-1)begin wave_seen<=0;result_seen<=0;segment_break<=1;breaks<=breaks+1'b1;end
   if(frame_valid && frame_ready)begin
    kind<=frame_type;length<=frame_length;index<=0;collecting<=frame_length!=0;
   end
   if(payload_valid && payload_ready)begin
    index<=index+1'b1;
    if(payload_last)finalize<=1;
   end
   if(finalize)begin
     collecting<=0;finalize<=0;
     if(index!=length)rejected<=rejected+1'b1;
     else if(kind==8'h40 && length==104 && wave_ok && lanes_ok)begin
      raw_mv<=complete[255:128];filtered_q8<=complete[447:256];fast_q4<=complete[639:448];slow_q4<=complete[831:640];
      sample_seq<=complete[63:32];sample_rate<=complete[79:64];wave_flags<=complete[31:24];
      generation<=complete[95:80];epoch<=complete[111:96];wave_gen<=complete[95:80];wave_epoch<=complete[111:96];
      if(!wave_seen || stale || complete[24] || complete[63:32]!=sample_seq+32'd1 ||
       complete[95:80]!=wave_gen || complete[111:96]!=wave_epoch ||
       (result_seen && (complete[95:80]!=result_gen || complete[111:96]!=result_epoch)))begin
       segment_break<=1;breaks<=breaks+1'b1;
      end
      wave_seen<=1;age<=0;wave_valid<=1;accepted<=accepted+1'b1;
     end else if(kind==8'h41 && length==32 && result_ok)begin
      class_id<=r[31:16];action_hint<=r[47:32];margin<=r[63:48];window_seq<=r[95:64];
      generation<=r[111:96];class_count<=r[127:112];epoch<=r[143:128];result_flags<=r[15:8];
      cnn_cycles<=r[191:160];head_cycles<=r[223:192];result_gen<=r[111:96];result_epoch<=r[143:128];
      if((wave_seen && (r[111:96]!=wave_gen || r[143:128]!=wave_epoch)) ||
       (result_seen && (r[111:96]!=result_gen || r[143:128]!=result_epoch)))begin
       segment_break<=1;wave_seen<=0;breaks<=breaks+1'b1;
      end
      result_seen<=1;age<=0;result_valid<=1;accepted<=accepted+1'b1;
     end else if(kind!=8'h11)rejected<=rejected+1'b1;
   end
  end
 end
endmodule
`default_nettype wire
