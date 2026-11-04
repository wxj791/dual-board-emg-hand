`timescale 1ns/1ps
`default_nettype none
// Matches EMG Studio V3.0: 8 little-endian float32 mV + 00 00 80 7F.
// Discards nonfinite/diagnostic frames. Applies the exact existing channel
// map [3,2,1,0,4,5,6,7] ONCE, then np.rint (ties-to-even) and clamp0..3300.
module justfloat_ingress #(
    parameter integer CLKS_PER_BIT=26,
    parameter integer GAP_CYCLES=6000000
) (
    input wire clk,input wire rst_n,input wire enable,input wire rx,input wire [31:0] timestamp_us,
    output wire sample_valid,input wire sample_ready,output wire [127:0] sample_data,
    output wire [15:0] sample_seq,output wire [31:0] sample_timestamp,
    output reg discontinuity,output reg [31:0] good_frames,output reg [31:0] bad_frames,
    output reg [31:0] diagnostic_frames,output reg [31:0] overflows
);
    wire [7:0] byte_data;wire byte_valid,framing,busy;
    ingress_uart_rx #(.CLKS_PER_BIT(CLKS_PER_BIT)) receiver(clk,rst_n,rx,byte_data,byte_valid,framing,busy);
    reg [287:0] shift;
    wire [287:0] shifted={byte_data,shift[287:8]};
    reg [5:0] byte_count;
    reg [255:0] payload;
    reg converting;reg [2:0] channel;reg [127:0] converted;
    reg [31:0] frame_timestamp;
    wire [2:0] source_channel=channel<4 ? 3'd3-channel : channel;
    wire [31:0] value=payload[source_channel*32 +:32];
    reg finite;
    integer f;
    always @* begin
        finite=1;
        for(f=0;f<8;f=f+1) if(shifted[f*32+23 +:8]==8'hff) finite=0;
    end
    wire diagnostic=shifted[31:0]>=32'hc7f11fc1 && shifted[31:0]<=32'hc7f1203f;
 function [15:0] millivolts;
  input [31:0] x;reg [23:0] sig;reg [11:0] whole;reg increment;reg [12:0] rounded;
  begin
   sig={1'b1,x[22:0]};whole=0;increment=0;rounded=0;
   if(x[31] || x[30:23]<126) millivolts=0;
   else if(x[30:23]>=139) millivolts=3300;
   else begin
    case(x[30:23])
     8'd126:begin whole=12'b0;increment=sig[23] && ((|sig[22:0]) || whole[0]);end
     8'd127:begin whole=sig[23:23];increment=sig[22] && ((|sig[21:0]) || whole[0]);end
     8'd128:begin whole=sig[23:22];increment=sig[21] && ((|sig[20:0]) || whole[0]);end
     8'd129:begin whole=sig[23:21];increment=sig[20] && ((|sig[19:0]) || whole[0]);end
     8'd130:begin whole=sig[23:20];increment=sig[19] && ((|sig[18:0]) || whole[0]);end
     8'd131:begin whole=sig[23:19];increment=sig[18] && ((|sig[17:0]) || whole[0]);end
     8'd132:begin whole=sig[23:18];increment=sig[17] && ((|sig[16:0]) || whole[0]);end
     8'd133:begin whole=sig[23:17];increment=sig[16] && ((|sig[15:0]) || whole[0]);end
     8'd134:begin whole=sig[23:16];increment=sig[15] && ((|sig[14:0]) || whole[0]);end
     8'd135:begin whole=sig[23:15];increment=sig[14] && ((|sig[13:0]) || whole[0]);end
     8'd136:begin whole=sig[23:14];increment=sig[13] && ((|sig[12:0]) || whole[0]);end
     8'd137:begin whole=sig[23:13];increment=sig[12] && ((|sig[11:0]) || whole[0]);end
     8'd138:begin whole=sig[23:12];increment=sig[11] && ((|sig[10:0]) || whole[0]);end
     default:begin whole=0;increment=0;end
    endcase
    rounded={1'b0,whole}+increment;
    millivolts=rounded>3300 ? 16'd3300 : {3'b0,rounded};
   end
  end
 endfunction
    reg [175:0] fifo[0:7];
    reg [2:0] read_pointer,write_pointer;reg [3:0] level;
    reg [15:0] sample_sequence;
    reg [23:0] gap_timer;reg have_sample;
    wire pop=sample_valid && sample_ready;
    wire push=converting && channel==7 && enable;
    wire full=level==8 && !pop;
    assign sample_valid=level!=0 && enable;
    assign {sample_timestamp,sample_seq,sample_data}=fifo[read_pointer];
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            shift<=0;byte_count<=0;payload<=0;converting<=0;channel<=0;converted<=0;frame_timestamp<=0;
            read_pointer<=0;write_pointer<=0;level<=0;sample_sequence<=0;gap_timer<=0;have_sample<=0;
            discontinuity<=0;good_frames<=0;bad_frames<=0;diagnostic_frames<=0;overflows<=0;
        end else begin
            discontinuity<=0;
            if(enable && gap_timer<GAP_CYCLES) gap_timer<=gap_timer+1'b1;
            if(!enable) begin
                shift<=0;byte_count<=0;converting<=0;read_pointer<=0;write_pointer<=0;level<=0;gap_timer<=0;have_sample<=0;
            end else if(framing) begin
                shift<=0;byte_count<=0;converting<=0;read_pointer<=0;write_pointer<=0;level<=0;
                bad_frames<=bad_frames+1'b1;discontinuity<=1;
            end else begin
                case({push && !full,pop})
                    2'b10:level<=level+1'b1;
                    2'b01:level<=level-1'b1;
                endcase
                if(pop) read_pointer<=read_pointer+1'b1;
                if(converting) begin
                    converted[channel*16 +:16]<=millivolts(value);
                    if(channel==7) begin
                        converting<=0;
                        if(full) begin
                            overflows<=overflows+1'b1;discontinuity<=1;
                            read_pointer<=0;write_pointer<=0;level<=0;
                        end else begin
                            fifo[write_pointer]<={frame_timestamp,sample_sequence,millivolts(value),converted[111:0]};
                            write_pointer<=write_pointer+1'b1;sample_sequence<=sample_sequence+1'b1;good_frames<=good_frames+1'b1;
                        end
                    end else channel<=channel+1'b1;
                end
                if(byte_valid) begin
                    shift<=shifted;if(byte_count<36) byte_count<=byte_count+1'b1;
                    if(shifted[287:256]==32'h7f800000) begin
                        byte_count<=0;
                        if(byte_count<35 || !finite || converting) begin bad_frames<=bad_frames+1'b1;end
                        else if(diagnostic) diagnostic_frames<=diagnostic_frames+1'b1;
                        else begin
                            payload<=shifted[255:0];converting<=1;channel<=0;frame_timestamp<=timestamp_us;
                            gap_timer<=0;have_sample<=1;
                            if(have_sample && gap_timer>=GAP_CYCLES) begin
                                discontinuity<=1;read_pointer<=0;write_pointer<=0;level<=0;
                            end
                        end
                    end
                end
            end
        end
    end
endmodule
`default_nettype wire
