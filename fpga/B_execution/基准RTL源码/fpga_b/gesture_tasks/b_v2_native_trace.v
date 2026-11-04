`timescale 1ns/1ps
`default_nettype none
// Full signed16 history, Q8 amplitude, pixel-square/one-pixel-strip area coverage.
// 256 coverage levels with linear-light sRGB blending; stages 0..18.
module pcs_native_trace(
 input wire clk,rst_n,sample_valid,
 input wire [127:0] sample_data,
 input wire [9:0] write_ptr,view_base,
 input wire raw_page,
 input wire mean_read,input wire [8:0] mean_even_addr,mean_odd_addr,
 output wire [127:0] mean_even_data,mean_odd_data,
 input wire [127:0] centers,ranges,gains,input wire [7:0] custom,
 input wire wave_valid,view_commit,history_clear,output reg clipped,
 input wire [11:0] h,v,
 input wire [8:0] sample_index,
 input wire [4:0] sample_phase,
 output reg [23:0] rgb,
 output reg active
);
 (* ram_style="block" *) reg [127:0] ring_even[0:511];
 (* ram_style="block" *) reg [127:0] ring_odd[0:511];
 (* ram_style="block" *) reg [23:0] normalization[0:1023];
 (* ram_style="bram_32k" *) reg [7:0] coverage[0:8191];
 (* ram_style="block" *) reg [23:0] colours[0:2047];
 initial begin
  $readmemh("fpga_b/pc_scope_1080_fine_exp/assets/normalization.hex",normalization);
  $readmemh("fpga_b/pc_scope_1080_fine_exp/assets/coverage.hex",coverage);
  $readmemh("fpga_b/pc_scope_1080_fine_exp/assets/trace_colours.hex",colours);
 end
 reg [8:0] index0;
 wire [7:0] band_decode={v>=779,v>=689 && v<779,v>=599 && v<689,v>=509 && v<599,v>=419 && v<509,v>=329 && v<419,v>=239 && v<329,v<239};
 wire [2:0] channel_decode={|band_decode[7:4],|(band_decode & 8'hcc),|(band_decode & 8'haa)};
 reg [7:0] band0;
 reg [6:0] lane_candidates0[0:7];
 always @(posedge clk)begin
  band0<=band_decode;
  lane_candidates0[0]<=v[6:0]-7'd21;
  lane_candidates0[1]<=v[6:0]-7'd111;
  lane_candidates0[2]<=v[6:0]-7'd73;
  lane_candidates0[3]<=v[6:0]-7'd35;
  lane_candidates0[4]<=v[6:0]-7'd125;
  lane_candidates0[5]<=v[6:0]-7'd87;
  lane_candidates0[6]<=v[6:0]-7'd49;
  lane_candidates0[7]<=v[6:0]-7'd11;
 end
 reg [2:0] channel[0:20];reg [6:0] lane[0:9];
 reg [4:0] phase[0:9];reg [5:0] inv_phase[0:9];
 reg [20:0] region_pipe,valid_pipe,mid_grid,edge_grid;
 integer i;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin
   region_pipe<=0;valid_pipe<=0;mid_grid<=0;edge_grid<=0;
  end else begin
   region_pipe<={region_pipe[19:0],h>=384 && h<2112 && v>=149 && v<869};
   valid_pipe<={valid_pipe[19:0],wave_valid};
   mid_grid<={mid_grid[19:0],(v==194 || v==284 || v==374 || v==464 || v==554 || v==644 || v==734 || v==824) || (sample_index[5:0]==0 && sample_phase==0)};
   edge_grid<={edge_grid[19:0],(v==149 || v==239 || v==329 || v==419 || v==509 || v==599 || v==689 || v==779)};
  end
 // Data-only pipelines flush before region/valid can reach the output.
 // Removing their asynchronous resets reduces recovery fanout at 148.5MHz.
 integer data_i;
 always @(posedge clk)begin
  index0<=sample_index;channel[0]<=channel_decode;
  phase[0]<=sample_phase;inv_phase[0]<=6'd27-{1'b0,sample_phase};
  for(data_i=1;data_i<21;data_i=data_i+1)channel[data_i]<=channel[data_i-1];
  lane[1]<=({7{band0[0]}} & lane_candidates0[0]) | ({7{band0[1]}} & lane_candidates0[1]) | ({7{band0[2]}} & lane_candidates0[2]) | ({7{band0[3]}} & lane_candidates0[3]) | ({7{band0[4]}} & lane_candidates0[4]) | ({7{band0[5]}} & lane_candidates0[5]) | ({7{band0[6]}} & lane_candidates0[6]) | ({7{band0[7]}} & lane_candidates0[7]);
  for(data_i=2;data_i<10;data_i=data_i+1)lane[data_i]<=lane[data_i-1];
  for(data_i=1;data_i<10;data_i=data_i+1)begin phase[data_i]<=phase[data_i-1];inv_phase[data_i]<=inv_phase[data_i-1];end
 end
 // Split the modulo-1024 sum by parity. Even/odd RAM addresses are
 // ceil/floor((view_base+index0)/2); avoid add10 -> increment -> mux.
 wire [8:0] half_index={1'b0,index0[8:1]};
 // Append carry-in as a low bit: ONE adder, no conditional increment.
 wire [9:0] odd_sum={view_base[9:1],1'b1}+{half_index,(view_base[0] & index0[0])};
 wire [9:0] even_sum={view_base[9:1],1'b1}+{half_index,(view_base[0] | index0[0])};
 wire [8:0] odd_address=odd_sum[9:1],even_address=even_sum[9:1];
 wire address_odd=view_base[0]^index0[0];
 assign mean_even_data=even1;assign mean_odd_data=odd1;
 reg [127:0] even1,odd1,data_a2,data_b2;reg odd_first1;
 reg last1,last2;
 reg signed [15:0] sample_a3,sample_b3;
 reg signed [40:0] scale_a4,scale_b4;
 reg signed [33:0] scale_low_a4,scale_low_b4;
 reg signed [23:0] scale_high_a4,scale_high_b4;
 reg [23:0] coef3;
 reg [23:0] lane_coef[0:7];
 reg [2:0] cfg_lane,cfg_lane1,cfg_lane2;
 reg [15:0] cfg_gain,cfg_gain2,cfg_range;
 reg [23:0] cfg_standard;
 reg [5:0] cfg_match;
 reg [2:0] cfg_lane3;reg [15:0] cfg_gain3;reg cfg_custom3;
 reg cfg_custom,cfg_custom2;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin cfg_lane<=0;cfg_lane1<=0;cfg_lane2<=0;cfg_gain<=0;cfg_gain2<=0;cfg_range<=0;cfg_match<=0;cfg_standard<=0;cfg_custom<=0;cfg_custom2<=0;cfg_lane3<=0;cfg_gain3<=0;cfg_custom3<=0;end
  else begin
   cfg_lane<=cfg_lane+1'b1;cfg_lane1<=cfg_lane;cfg_lane2<=cfg_lane1;
   cfg_gain<=gains[cfg_lane*16+:16];cfg_range<=ranges[cfg_lane*16+:16];cfg_custom<=custom[cfg_lane];
   cfg_match<={cfg_range==1200,cfg_range==500,
    cfg_range==200,cfg_range==100,
    cfg_range==50,cfg_range==20};
   cfg_standard<=({24{cfg_match[0]}} & 24'd2149581) |
    ({24{cfg_match[1]}} & 24'd859832) | ({24{cfg_match[2]}} & 24'd429916) |
    ({24{cfg_match[3]}} & 24'd214958) | ({24{cfg_match[4]}} & 24'd85983) |
    ({24{cfg_match[5]}} & 24'd35826) | ({24{!(|cfg_match)}} & 24'd13028);cfg_gain2<=cfg_gain;cfg_custom2<=cfg_custom;
   cfg_lane3<=cfg_lane2;cfg_gain3<=cfg_gain2;cfg_custom3<=cfg_custom2;
  end
 always @(posedge clk)lane_coef[cfg_lane3]<=cfg_custom3?cfg_gain3*24'd117:cfg_standard;
 reg [15:0] lane_a2,lane_b2,center2;
 reg [127:0] even_ram2,odd_ram2;
 reg odd_ramfirst2,last_ram2;
 always @(posedge clk)begin
  even_ram2<=even1;odd_ram2<=odd1;odd_ramfirst2<=odd_first1;last_ram2<=last1;
 end
 reg [15:0] even_read2,odd_read2;reg odd_first2,last_read2;
 always @(posedge clk)begin
  even_read2<=even_ram2[channel[2]*16+:16];odd_read2<=odd_ram2[channel[2]*16+:16];odd_first2<=odd_ramfirst2;last_read2<=last_ram2;
 end
 always @(posedge clk)begin
  lane_a2<=odd_first2?odd_read2:even_read2;
  lane_b2<=last_read2?(odd_first2?odd_read2:even_read2):(odd_first2?even_read2:odd_read2);
  center2<=centers[channel[3]*16+:16];
 end
 function [23:0] raw_coef;input [15:0] r;
  begin case(r)
   20:raw_coef=2149581;50:raw_coef=859832;100:raw_coef=429916;
   200:raw_coef=214958;500:raw_coef=85983;1200:raw_coef=35826;default:raw_coef=13028;
  endcase end
 endfunction
 reg signed [14:0] q_a5,q_b5;
 reg high_a5,low_a5,high_b5,low_b5;
 reg clip_accum;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin clip_accum<=0;clipped<=0;end
  else if(history_clear)begin clip_accum<=0;clipped<=0;end
  else if(view_commit)begin clipped<=clip_accum;clip_accum<=0;end
  else if(region_pipe[8] && valid_pipe[8] && (high_a5 || low_a5 || high_b5 || low_b5))clip_accum<=1;
 end
 reg [14:0] amplitude_a6,amplitude_b6;
 reg [19:0] product_a7,product_b7,row7,numerator8,row8;
 reg signed [15:0] slope7;
 reg [15:0] slope8;
 wire [16:0] rounded_slope={1'b0,slope8}+17'd16;
 reg signed [20:0] error9;
 reg [23:0] normal9,normal10;
 reg [20:0] magnitude10;
 reg [35:0] low11;reg [36:0] low12;
 reg [17:0] cross_a11;reg [20:0] cross_b11;reg [2:0] high11;
 reg [21:0] cross12;
 reg [39:0] distance13;
 wire [40:0] rounded_distance={1'b0,distance13}+41'd8388608;
 reg [4:0] angle11,angle12,angle13,angle14;
 reg [7:0] distance14;
 reg [7:0] coverage15,coverage16;
 reg [23:0] colour17;
 reg line17;
 always @(posedge clk)begin
  if(sample_valid && !write_ptr[0])ring_even[write_ptr[9:1]]<=sample_data;
  if(sample_valid && write_ptr[0])ring_odd[write_ptr[9:1]]<=sample_data;
  even1<=ring_even[mean_read?mean_even_addr:even_address];odd1<=ring_odd[mean_read?mean_odd_addr:odd_address];odd_first1<=address_odd;last1<=index0==511;
  data_a2<=odd_first1?odd1:even1;data_b2<=odd_first1?even1:odd1;last2<=last1;
  sample_a3<=raw_page?$signed({1'b0,lane_a2})-$signed({1'b0,center2}):lane_a2;
  coef3<=raw_page?lane_coef[channel[4]]:24'd1872;
  sample_b3<=raw_page?$signed({1'b0,lane_b2})-$signed({1'b0,center2}):lane_b2;
  scale_low_a4<=sample_a3*$signed({1'b0,coef3[16:0]});
  scale_low_b4<=sample_b3*$signed({1'b0,coef3[16:0]});
  scale_high_a4<=sample_a3*$signed({1'b0,coef3[23:17]});
  scale_high_b4<=sample_b3*$signed({1'b0,coef3[23:17]});
  scale_a4<={{7{scale_low_a4[33]}},scale_low_a4}+{scale_high_a4,17'd0};
  scale_b4<={{7{scale_low_b4[33]}},scale_low_b4}+{scale_high_b4,17'd0};
  q_a5<=($signed(scale_a4)>>>12);q_b5<=($signed(scale_b4)>>>12);
  high_a5<=$signed(($signed(scale_a4)>>>12))>15'sd10496;low_a5<=$signed(($signed(scale_a4)>>>12))< -15'sd10496;
  high_b5<=$signed(($signed(scale_b4)>>>12))>15'sd10496;low_b5<=$signed(($signed(scale_b4)>>>12))< -15'sd10496;
  amplitude_a6<=high_a5?15'd1024:low_a5?15'd22016:(15'd11520-q_a5);
  amplitude_b6<=high_b5?15'd1024:low_b5?15'd22016:(15'd11520-q_b5);
  product_a7<=amplitude_a6*inv_phase[9];product_b7<=amplitude_b6*phase[9];
  row7<=lane[9]*20'd6912;
  slope7<=$signed({1'b0,amplitude_b6})-$signed({1'b0,amplitude_a6});
  numerator8<=product_a7+product_b7;row8<=row7;
  slope8<=slope7[15]?-slope7:slope7;
  error9<=$signed({1'b0,numerator8})-$signed({1'b0,row8});
  normal9<=normalization[(slope8[14:5]+{9'd0,slope8[4]})];
  magnitude10<=error9[20]?-error9:error9;normal10<=normal9;
  // Split the 21x19 product at registered partial products; no cascaded
  // wide multiply/add path must finish within a single 148.5MHz cycle.
  low11<=magnitude10[17:0]*normal10[17:0];
  cross_a11<=normal10[18]?magnitude10[17:0]:18'd0;
  cross_b11<=magnitude10[20:18]*normal10[17:0];
  high11<=normal10[18]?magnitude10[20:18]:3'd0;
  angle11<=normal10[23:19];
  low12<={1'b0,low11}+37'd8388608;cross12<={4'd0,cross_a11}+{1'b0,cross_b11}+{1'b0,high11,18'd0};angle12<=angle11;
  distance13<={3'd0,low12}+{cross12,18'd0};angle13<=angle12;
  distance14<=|distance13[39:32]?8'd255:distance13[31:24];angle14<=angle13;
  coverage15<=coverage[{angle14,distance14}];
  coverage16<=coverage15;
  colour17<=colours[{channel[19],coverage16}];line17<=coverage16!=0;
 end
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin rgb<=0;active<=0;end
  else begin
   active<=region_pipe[20];
   if(line17 && valid_pipe[20])rgb<=colour17;
   else if(edge_grid[20])rgb<=24'h304058;
   else if(mid_grid[20])rgb<=24'h203044;
   else rgb<=24'h080c18;
  end
endmodule
`default_nettype wire
