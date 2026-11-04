`timescale 1ns/1ps
`default_nettype none
// Isolated layout: 8 x 40px traces above; servo table left and 24-frame whale right; one common background.
module b_v2_renderer(
 input wire clk,rst_n,input wire sample_valid,input wire [127:0] sample_data,
 input wire [319:0] servo_snapshot,
 input wire history_clear,freeze,input wire [2:0] page,
 input wire [15:0] gain,sample_rate,class_id,generation,mapped_action,mapped_pose,
 input wire [127:0] channel_gains,ranges,input wire [7:0] custom,
 input wire armed,saturated,link_stale,
 output reg [7:0] vga_r,vga_g,vga_b,output reg vga_hs,vga_vs,
 output wire vga_clk,frame_start,output reg [31:0] sample_count,frame_count,
 output reg wave_valid,output wire [9:0] debug_x,output wire [8:0] debug_y,
 output wire debug_de,output wire [4:0] animation_frame
);
 wire hs,vs,de;wire [9:0] x;wire [11:0] h,v;wire [8:0] y;wire [8:0] wave_x;wire [4:0] wave_phase;
 vga_timing timing(.clk(clk),.rst_n(rst_n),.h(h),.v(v),.hs(hs),.vs(vs),
  .de(de),.x(x),.y(y),.frame_start(frame_start),.wave_x(wave_x),.wave_phase(wave_phase));
 assign vga_clk=~clk;
 reg [9:0] write_ptr,view_base,history;
 reg [127:0] sample_data1;reg sample_valid1;
 reg [127:0] centers;
 reg [21:0] mean_sum[0:7];
 reg [3:0] mean_state;
 reg [7:0] mean_pair;
 reg [2:0] mean_lane;
 reg [8:0] mean_even_addr,mean_odd_addr;
 reg [127:0] mean_even_word,mean_odd_word;
 reg [15:0] mean_even_lane,mean_odd_lane;
 reg [16:0] mean_pair_value;
 reg mean_bad,mean_fault;
 wire [127:0] mean_even_data,mean_odd_data;
 wire mean_busy=mean_state!=0;
 wire mean_read=mean_busy;
 wire [9:0] mean_base=write_ptr-10'd512;
 integer mean_i;
 // Read the existing 1024-point history only during vertical blanking.
 // Eight channels share one sum datapath. Every scan begins from zero;
 // no read/write collision or old differential sum can cause drift.
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   centers<=0;mean_state<=0;mean_pair<=0;mean_lane<=0;
   mean_even_addr<=0;mean_odd_addr<=0;mean_bad<=0;mean_fault<=0;
   for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
  end else if(history_clear)begin
   centers<=0;mean_state<=0;mean_bad<=0;mean_fault<=0;
   for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
  end else if(mean_busy && v>=41)begin
   mean_state<=0;mean_fault<=1;
  end else case(mean_state)
   0:if(view_commit && page==0 && history>=512 && !freeze && v<41)begin
    mean_even_addr<=mean_base[9:1]+mean_base[0];mean_odd_addr<=mean_base[9:1];
    mean_pair<=0;mean_lane<=0;mean_bad<=0;mean_state<=1;
    for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
   end
   1:mean_state<=2;
   2:mean_state<=3;
   3:begin mean_even_word<=mean_even_data;mean_odd_word<=mean_odd_data;mean_lane<=0;mean_state<=4;end
   4:begin
    mean_even_lane<=mean_even_word[mean_lane*16+:16];mean_odd_lane<=mean_odd_word[mean_lane*16+:16];mean_state<=5;
   end
   5:begin
    mean_pair_value<={1'b0,mean_even_lane}+{1'b0,mean_odd_lane};
    if(mean_even_lane>3300 || mean_odd_lane>3300)mean_bad<=1;
    mean_state<=6;
   end
   6:begin
    mean_sum[mean_lane]<=mean_sum[mean_lane]+mean_pair_value;
    if(mean_lane!=7)begin mean_lane<=mean_lane+1'b1;mean_state<=4;end
    else if(mean_pair==255)mean_state<=7;
    else begin
     mean_pair<=mean_pair+1'b1;mean_even_addr<=mean_even_addr+1'b1;mean_odd_addr<=mean_odd_addr+1'b1;mean_state<=1;
    end
   end
   7:begin
    if(!mean_bad)for(mean_i=0;mean_i<8;mean_i=mean_i+1)centers[mean_i*16+:16]<=mean_sum[mean_i]>>9;
    else mean_fault<=1;
    mean_state<=0;
   end
   default:begin mean_state<=0;mean_fault<=1;end
  endcase
 end
 reg view_waiting;
 wire view_commit=(frame_start || view_waiting) && !sample_valid1 && !mean_busy;
 always @(posedge clk or negedge rst_n)if(!rst_n)view_waiting<=0;else if(history_clear)view_waiting<=0;else if(view_commit)view_waiting<=0;else if(frame_start)view_waiting<=1;

 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin sample_valid1<=0;end
  else begin sample_valid1<=sample_valid && !freeze && !history_clear && page<4;end
 always @(posedge clk)sample_data1<=sample_data;
 wire [23:0] native_rgb;wire native_active,native_clip;
 pcs_native_trace native_trace(.clk(clk),.rst_n(rst_n),.sample_valid(sample_valid1 && !history_clear),
  .sample_data(sample_data1),.raw_page(page==0),.mean_read(mean_read),.mean_even_addr(mean_even_addr),.mean_odd_addr(mean_odd_addr),.mean_even_data(mean_even_data),.mean_odd_data(mean_odd_data),.centers(centers),.ranges(ranges),.gains(channel_gains),.custom(custom),.write_ptr(write_ptr),.view_base(view_base),.wave_valid(wave_valid),.view_commit(view_commit),.history_clear(history_clear),.clipped(native_clip),
  .h(h),.v(v),.sample_index(wave_x),.sample_phase(wave_phase),.rgb(native_rgb),.active(native_active));
 // Coordinate stage ends the raster subtract/decode before display addressing.
 reg [9:0] x0;reg [8:0] y0,wave_x0;reg hs0,vs0,de0;
 always @(posedge clk)begin x0<=x;y0<=y;wave_x0<=wave_x;hs0<=hs;vs0<=vs;de0<=de;end
 b_v2_whale_player whale_player(.clk(clk),.rst_n(rst_n),.frame_start(frame_start),.frame(animation_frame));
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin write_ptr<=0;view_base<=0;history<=0;sample_count<=0;frame_count<=0;
   wave_valid<=0;end
  else begin
   if(sample_valid1)begin write_ptr<=write_ptr+1'b1;sample_count<=sample_count+1'b1;
    if(history<512)history<=history+1'b1;end
   if(history_clear)begin write_ptr<=0;view_base<=0;history<=0;wave_valid<=0;end
   if(view_commit && !history_clear)begin
    frame_count<=frame_count+1'b1;
    if(history>=512)begin view_base<=write_ptr-10'd512;wave_valid<=1;end
   end
  end
 wire [23:0] sprite_rgb;
 // 64 stored pixels -> 112 displayed pixels. Scaling changes addresses, not ROM depth.
 // Right-hand 256x112 region: center the largest square without shrinking traces.
 reg [6:0] sprite_dx,sprite_dy;
 always @(posedge clk)begin sprite_dx<=x-10'd456;sprite_dy<=y-9'd368;end
 function [5:0] sprite_scale;input [6:0] d;
  begin case(d)
   7'd0:sprite_scale=6'd0;
   7'd1:sprite_scale=6'd0;
   7'd2:sprite_scale=6'd1;
   7'd3:sprite_scale=6'd1;
   7'd4:sprite_scale=6'd2;
   7'd5:sprite_scale=6'd2;
   7'd6:sprite_scale=6'd3;
   7'd7:sprite_scale=6'd4;
   7'd8:sprite_scale=6'd4;
   7'd9:sprite_scale=6'd5;
   7'd10:sprite_scale=6'd5;
   7'd11:sprite_scale=6'd6;
   7'd12:sprite_scale=6'd6;
   7'd13:sprite_scale=6'd7;
   7'd14:sprite_scale=6'd8;
   7'd15:sprite_scale=6'd8;
   7'd16:sprite_scale=6'd9;
   7'd17:sprite_scale=6'd9;
   7'd18:sprite_scale=6'd10;
   7'd19:sprite_scale=6'd10;
   7'd20:sprite_scale=6'd11;
   7'd21:sprite_scale=6'd12;
   7'd22:sprite_scale=6'd12;
   7'd23:sprite_scale=6'd13;
   7'd24:sprite_scale=6'd13;
   7'd25:sprite_scale=6'd14;
   7'd26:sprite_scale=6'd14;
   7'd27:sprite_scale=6'd15;
   7'd28:sprite_scale=6'd16;
   7'd29:sprite_scale=6'd16;
   7'd30:sprite_scale=6'd17;
   7'd31:sprite_scale=6'd17;
   7'd32:sprite_scale=6'd18;
   7'd33:sprite_scale=6'd18;
   7'd34:sprite_scale=6'd19;
   7'd35:sprite_scale=6'd20;
   7'd36:sprite_scale=6'd20;
   7'd37:sprite_scale=6'd21;
   7'd38:sprite_scale=6'd21;
   7'd39:sprite_scale=6'd22;
   7'd40:sprite_scale=6'd22;
   7'd41:sprite_scale=6'd23;
   7'd42:sprite_scale=6'd24;
   7'd43:sprite_scale=6'd24;
   7'd44:sprite_scale=6'd25;
   7'd45:sprite_scale=6'd25;
   7'd46:sprite_scale=6'd26;
   7'd47:sprite_scale=6'd26;
   7'd48:sprite_scale=6'd27;
   7'd49:sprite_scale=6'd28;
   7'd50:sprite_scale=6'd28;
   7'd51:sprite_scale=6'd29;
   7'd52:sprite_scale=6'd29;
   7'd53:sprite_scale=6'd30;
   7'd54:sprite_scale=6'd30;
   7'd55:sprite_scale=6'd31;
   7'd56:sprite_scale=6'd32;
   7'd57:sprite_scale=6'd32;
   7'd58:sprite_scale=6'd33;
   7'd59:sprite_scale=6'd33;
   7'd60:sprite_scale=6'd34;
   7'd61:sprite_scale=6'd34;
   7'd62:sprite_scale=6'd35;
   7'd63:sprite_scale=6'd36;
   7'd64:sprite_scale=6'd36;
   7'd65:sprite_scale=6'd37;
   7'd66:sprite_scale=6'd37;
   7'd67:sprite_scale=6'd38;
   7'd68:sprite_scale=6'd38;
   7'd69:sprite_scale=6'd39;
   7'd70:sprite_scale=6'd40;
   7'd71:sprite_scale=6'd40;
   7'd72:sprite_scale=6'd41;
   7'd73:sprite_scale=6'd41;
   7'd74:sprite_scale=6'd42;
   7'd75:sprite_scale=6'd42;
   7'd76:sprite_scale=6'd43;
   7'd77:sprite_scale=6'd44;
   7'd78:sprite_scale=6'd44;
   7'd79:sprite_scale=6'd45;
   7'd80:sprite_scale=6'd45;
   7'd81:sprite_scale=6'd46;
   7'd82:sprite_scale=6'd46;
   7'd83:sprite_scale=6'd47;
   7'd84:sprite_scale=6'd48;
   7'd85:sprite_scale=6'd48;
   7'd86:sprite_scale=6'd49;
   7'd87:sprite_scale=6'd49;
   7'd88:sprite_scale=6'd50;
   7'd89:sprite_scale=6'd50;
   7'd90:sprite_scale=6'd51;
   7'd91:sprite_scale=6'd52;
   7'd92:sprite_scale=6'd52;
   7'd93:sprite_scale=6'd53;
   7'd94:sprite_scale=6'd53;
   7'd95:sprite_scale=6'd54;
   7'd96:sprite_scale=6'd54;
   7'd97:sprite_scale=6'd55;
   7'd98:sprite_scale=6'd56;
   7'd99:sprite_scale=6'd56;
   7'd100:sprite_scale=6'd57;
   7'd101:sprite_scale=6'd57;
   7'd102:sprite_scale=6'd58;
   7'd103:sprite_scale=6'd58;
   7'd104:sprite_scale=6'd59;
   7'd105:sprite_scale=6'd60;
   7'd106:sprite_scale=6'd60;
   7'd107:sprite_scale=6'd61;
   7'd108:sprite_scale=6'd61;
   7'd109:sprite_scale=6'd62;
   7'd110:sprite_scale=6'd62;
   7'd111:sprite_scale=6'd63;
   7'd112:sprite_scale=6'd0;
   7'd113:sprite_scale=6'd0;
   7'd114:sprite_scale=6'd1;
   7'd115:sprite_scale=6'd1;
   7'd116:sprite_scale=6'd2;
   7'd117:sprite_scale=6'd2;
   7'd118:sprite_scale=6'd3;
   7'd119:sprite_scale=6'd4;
   7'd120:sprite_scale=6'd4;
   7'd121:sprite_scale=6'd5;
   7'd122:sprite_scale=6'd5;
   7'd123:sprite_scale=6'd6;
   7'd124:sprite_scale=6'd6;
   7'd125:sprite_scale=6'd7;
   7'd126:sprite_scale=6'd8;
   7'd127:sprite_scale=6'd8;
   default:sprite_scale=0;
  endcase end
 endfunction
 reg [5:0] sprite_x_scaled,sprite_y_scaled;
 always @(posedge clk)begin sprite_x_scaled<=sprite_scale(sprite_dx);sprite_y_scaled<=sprite_scale(sprite_dy);end
 b_v2_whale_sprite sprite(.clk(clk),.frame(animation_frame),.x(sprite_x_scaled[5:0]),.y(sprite_y_scaled[5:0]),.rgb(sprite_rgb));
 reg [9:0] x1;reg [8:0] y1,wave_x1,wave_x2;reg hs1,vs1,de1;
 always @(posedge clk)begin x1<=x0;y1<=y0;wave_x1<=wave_x0;hs1<=hs0;vs1<=vs0;de1<=de0;end
 // UI observes the samples arriving in the pixel domain, including while frozen.
 reg wave_arrived,wave_seen;reg [5:0] idle_frames;reg flow_active;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin wave_arrived<=0;wave_seen<=0;idle_frames<=30;flow_active<=0;end
  else begin
   if(sample_valid)begin wave_arrived<=1;wave_seen<=1;end
   if(frame_start)begin
    wave_arrived<=sample_valid;
    if(wave_arrived || sample_valid)begin idle_frames<=0;flow_active<=1;end
    else if(idle_frames<30)begin idle_frames<=idle_frames+1'b1;if(idle_frames==29)flow_active<=0;end
   end
  end
 reg number_update;
 always @(posedge clk or negedge rst_n)if(!rst_n)number_update<=0;else number_update<=view_commit || mean_state==7;
 reg [12:0] number_write;
 reg [15:0] number_result;
 (* preserve="true" *) reg [15:0] number_result_center,number_result_position;
 reg [127:0] centers_bcd;
 wire [127:0] ranges_bcd;
 reg [79:0] positions_bcd;
 (* preserve="true" *) reg [12:0] number_slot;
 (* preserve="true" *) reg [18:0] number_phase;
 reg numbers_busy;
 reg [31:0] number_shift;
 reg [31:0] number_adjusted;
 reg [15:0] number_selected;
 integer number_k,number_source;
 always @*begin
  number_selected=0;
  for(number_source=0;number_source<8;number_source=number_source+1)begin
   number_selected=number_selected | ({16{number_slot[number_source]}} & centers[number_source*16+:16]);
  end
  for(number_source=0;number_source<5;number_source=number_source+1)begin
   number_selected=number_selected | ({16{number_slot[8+number_source]}} & servo_snapshot[16+number_source*16+:16]);
  end
  number_adjusted=number_shift;
  for(number_k=16;number_k<32;number_k=number_k+4)
   if(number_shift[number_k+:4]>=5)number_adjusted[number_k+:4]=number_shift[number_k+:4]+3;
 end
 reg [15:0] number_input;
 always @(posedge clk)number_input<=number_selected;
 integer number_destination;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   number_slot<=1;number_phase<=1;numbers_busy<=0;
   number_shift<=0;number_write<=0;number_result<=0;number_result_center<=0;number_result_position<=0;
  end else begin
   for(number_destination=0;number_destination<13;number_destination=number_destination+1)
    number_write[number_destination]<=numbers_busy && number_phase[18] && number_slot[number_destination];
   number_result<=number_shift[31:16];number_result_center<=number_shift[31:16];number_result_position<=number_shift[31:16];
   if(number_update)begin number_slot<=1;number_phase<=1;numbers_busy<=1;end
   else if(numbers_busy)begin
    number_phase<={number_phase[17:0],number_phase[18]};
    if(number_phase[1])number_shift<={16'd0,number_input};
    else if(|number_phase[17:2])number_shift<=number_adjusted<<1;
    if(number_phase[18])begin
     number_slot<={number_slot[11:0],1'b0};
     if(number_slot[12])numbers_busy<=0;
    end
   end
  end
 end
 genvar bcd_item;
 generate for(bcd_item=0;bcd_item<13;bcd_item=bcd_item+1)begin:bcd_destination
  if(bcd_item<8)begin
   always @(posedge clk or negedge rst_n)if(!rst_n)centers_bcd[bcd_item*16+:16]<=0;else if(number_write[bcd_item])centers_bcd[bcd_item*16+:16]<=number_result_center;
  end else begin
   always @(posedge clk or negedge rst_n)if(!rst_n)positions_bcd[(bcd_item-8)*16+:16]<=0;else if(number_write[bcd_item])positions_bcd[(bcd_item-8)*16+:16]<=number_result_position;
  end
 end endgenerate
 // Seven legal ranges have fixed decimal digits; no runtime conversion.
 function [15:0] range_decimal;input [15:0] mv;begin case(mv)
  20:range_decimal=16'h0020;50:range_decimal=16'h0050;
  100:range_decimal=16'h0100;200:range_decimal=16'h0200;
  500:range_decimal=16'h0500;1200:range_decimal=16'h1200;
  3300:range_decimal=16'h3300;default:range_decimal=16'hffff;
 endcase end endfunction
 generate for(bcd_item=0;bcd_item<8;bcd_item=bcd_item+1)begin:range_lookup
  assign ranges_bcd[bcd_item*16+:16]=range_decimal(ranges[bcd_item*16+:16]);
 end endgenerate
 function [6:0] hexchar;input [3:0] n;begin hexchar=n<10?48+n:65+n-10;end endfunction
 function [2:0] lane_id;input [8:0] yy;begin
  if(yy<88)lane_id=0;else if(yy<128)lane_id=1;else if(yy<168)lane_id=2;
  else if(yy<208)lane_id=3;else if(yy<248)lane_id=4;else if(yy<288)lane_id=5;
  else if(yy<328)lane_id=6;else lane_id=7;
 end endfunction
 function [5:0] lane_offset;input [8:0] yy;begin
  case(lane_id(yy))
   0:lane_offset=yy-48;1:lane_offset=yy-88;2:lane_offset=yy-128;3:lane_offset=yy-168;
   4:lane_offset=yy-208;5:lane_offset=yy-248;6:lane_offset=yy-288;7:lane_offset=yy-328;
  endcase
 end endfunction
 wire [6:0] col0=x0[9:3],col=x1[9:3];
 reg [2:0] channel_early;reg [5:0] lane_early;
 // Prepare only range, RAW mean and finger position digits.
 reg [31:0] range_digits,center_digits;reg [19:0] position_digits;
 integer digit_lane;
 wire [2:0] servo_row_input=y[6:4],servo_row=y0[6:4];
 reg servo_online0,servo_torque0,servo_feedback0,servo_torque_valid0;
 reg custom0;
 always @(posedge clk)begin
  channel_early<=lane_id(y);lane_early<=lane_offset(y);
  custom0<=custom[lane_id(y)];
  for(digit_lane=0;digit_lane<8;digit_lane=digit_lane+1)begin
   range_digits[digit_lane*4+:4]<=ranges_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
   center_digits[digit_lane*4+:4]<=centers_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
  end
  for(digit_lane=0;digit_lane<5;digit_lane=digit_lane+1)
   position_digits[digit_lane*4+:4]<=positions_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
  servo_online0<=servo_snapshot[256+servo_row_input];
  servo_torque0<=servo_snapshot[261+servo_row_input];
  servo_feedback0<=servo_snapshot[266+servo_row_input];
  servo_torque_valid0<=servo_snapshot[289+servo_row_input];
 end
 reg [4:0] text_region;
 reg [5:0] text_column;
 always @(posedge clk)begin
  text_region[0]<=y<16;
  text_region[1]<=y>=16 && y<32;
  text_region[2]<=y>=48 && y<368 && lane_offset(y)<16 && x<64;
  text_region[3]<=y>=48 && y<368 && lane_offset(y)>=16 && lane_offset(y)<32 && x<64;
  text_region[4]<=y>=384 && y<464 && x<384;
  text_column[0]<=x[9:3]==1;
  text_column[1]<=x[9:3]>=4 && x[9:3]<8;
  text_column[2]<=x[9:3]==2;
  text_column[3]<=x[9:3]>=8 && x[9:3]<12;
  text_column[4]<=x[9:3]==17;
  text_column[5]<=x[9:3]==23;
 end
 reg [2:0] template_id,template_id_early;
 reg text_enabled,text_enabled_early,text_enabled2;
 reg [23:0] text_colour,text_colour_early;
 reg dynamic_valid,dynamic_valid_early;
 reg [6:0] dynamic_terms[0:5];
 wire [6:0] dynamic_character=dynamic_terms[0]|dynamic_terms[1]|dynamic_terms[2]|dynamic_terms[3]|dynamic_terms[4]|dynamic_terms[5];
 wire range_active=text_region[2] && text_column[1];
 wire center_active=text_region[3] && page==0 && text_column[1];
 wire position_active=text_region[4] && text_column[3];
 wire channel_active=text_region[2] && text_column[0];
 wire finger_active=text_region[4] && text_column[2];
 wire status_active=text_region[1] && (col0==5 || col0==14 || col0==23 || col0==30);
 wire finger_flags=text_region[4] && (text_column[4] || text_column[5]);
 wire [3:0] range_nibble=range_digits[channel_early*4+:4];
 wire [6:0] custom_character=col0==4?7'd67:col0==5?7'd85:col0==6?7'd83:7'd84;
 wire [6:0] range_character=custom0?custom_character:hexchar(range_nibble);
 wire [6:0] center_character=hexchar(center_digits[channel_early*4+:4]);
 wire [6:0] position_character=servo_feedback0?hexchar(position_digits[servo_row*4+:4]):7'd63;
 reg [6:0] status_character;
 always @*begin
  status_character=7'd48;
  case(col0)
   5:status_character=freeze?7'd49:7'd48;
   14:status_character=(!link_stale && flow_active)?7'd49:7'd48;
   23:status_character=(saturated || native_clip)?7'd49:7'd48;
   30:status_character=armed?7'd49:7'd48;
  endcase
 end
 wire [6:0] finger_character=text_column[4]?(servo_online0?7'd49:7'd48):
  !servo_torque_valid0?7'd63:servo_torque0?7'd49:7'd48;
 always @*begin
  template_id_early=({3{text_region[0]}} & page[1:0]) |
   ({3{text_region[1]}} & 3'd4) | ({3{text_region[2]}} & 3'd5) |
   ({3{text_region[3]}} & 3'd6) | ({3{text_region[4]}} & 3'd7);
  text_enabled_early=text_region[0] || text_region[1] || text_region[2] ||
   (text_region[3] && page==0) || text_region[4];
  text_colour_early=text_region[2]?24'h65d9ef:24'h9cacc6;
  dynamic_valid_early=range_active || center_active || position_active || channel_active || finger_active || status_active || finger_flags;
 end
 always @(posedge clk)begin
  template_id<=template_id_early;text_enabled<=text_enabled_early;
  text_colour<=text_colour_early;dynamic_valid<=dynamic_valid_early;
  dynamic_terms[0]<=range_active?range_character:7'd0;
  dynamic_terms[1]<=center_active?center_character:7'd0;
  dynamic_terms[2]<=position_active?position_character:7'd0;
  dynamic_terms[3]<=channel_active?(7'd48+channel_early):finger_active?(7'd49+servo_row):7'd0;
  dynamic_terms[4]<=status_active?status_character:7'd0;
  dynamic_terms[5]<=finger_flags?finger_character:7'd0;
 end
 (* ram_style="block" *) reg [6:0] text_rows[0:1023];
 initial $readmemh("fpga_b/gesture_tasks/assets/text_rows_minimal.hex",text_rows);
 wire [2:0] font_row=(y1>=48 && y1<368 && col<8)?lane_offset(y1)>>1:y1[3:1];
 reg [6:0] static_character2,character3;reg dynamic_valid2;reg [6:0] dynamic_character2;
 reg [2:0] font_row2,font_row3,font_col2,font_col3,font_col4,font_col5;
 reg [23:0] base_rgb2,text_colour2,base_rgb3,text_colour3,base_rgb4,text_colour4,base_rgb5,text_colour5;
 reg hs2,vs2,de_mid,hs3,vs3,de3,hs4,vs4,de4,hs5,vs5,de5;
 reg [9:0] x_mid,x3,x4,x5;reg [8:0] y_mid,y3,y4,y5;
 always @(posedge clk)static_character2<=text_rows[{template_id,col}];
 wire [4:0] font_row_bits;reg [4:0] font_bits5;
 pcs_font_row font(.clk(clk),.character(character3),.row(font_row3),.row_bits(font_row_bits));
 wire font_dot=(font_col5<5)?font_bits5[4-font_col5]:1'b0;
 reg de6,hs6,vs6;
 reg [9:0] x6;reg [8:0] y6;
 reg [23:0] overlay_rgb6,native_rgb6;reg native_active6;
 always @(posedge clk)begin
  overlay_rgb6<=font_dot?text_colour5:base_rgb5;
  de6<=de5;hs6<=hs5;vs6<=vs5;x6<=x5;y6<=y5;
  native_rgb6<=native_rgb;native_active6<=native_active;
 end
 // Sprite: delta -> scale -> pixel BRAM -> LUT palette -> late composition.
 reg sprite_active2,sprite_active3,sprite_active4;
 always @(posedge clk)begin
  sprite_active2<=x1>=456 && x1<568 && y1>=368 && y1<480;
  sprite_active3<=sprite_active2;sprite_active4<=sprite_active3;
 end
 wire [23:0] rgb=24'h080c18;
 always @(posedge clk)begin dynamic_valid2<=dynamic_valid;text_enabled2<=text_enabled;dynamic_character2<=dynamic_character;font_row2<=font_row;font_col2<=x1[2:0];base_rgb2<=rgb;
   text_colour2<=text_colour;hs2<=hs1;vs2<=vs1;de_mid<=de1;x_mid<=x1;y_mid<=y1;end
 always @(posedge clk)begin character3<=!text_enabled2?7'd32:dynamic_valid2?dynamic_character2:static_character2;font_row3<=font_row2;font_col3<=font_col2;
   base_rgb3<=0;text_colour3<=text_colour2;hs3<=hs2;vs3<=vs2;de3<=de_mid;x3<=x_mid;y3<=y_mid;end
 always @(posedge clk)begin font_col4<=font_col3;base_rgb4<=rgb;text_colour4<=text_colour3;
   hs4<=hs3;vs4<=vs3;de4<=de3;x4<=x3;y4<=y3;end
 always @(posedge clk)begin font_bits5<=font_row_bits;font_col5<=font_col4;base_rgb5<=sprite_active4?sprite_rgb:base_rgb4;text_colour5<=text_colour4;
   hs5<=hs4;vs5<=vs4;de5<=de4;x5<=x4;y5<=y4;end
 reg [9:0] x2;reg [8:0] y2;reg de2;
 reg [7:0] overlay_r,overlay_g,overlay_b;reg overlay_hs,overlay_vs;
 reg [45:0] overlay_pipe[0:14];integer pipe_i;
 reg [9:0] final_x;reg [8:0] final_y;reg final_de;
 assign debug_x=final_x;assign debug_y=final_y;assign debug_de=final_de;
 // Local reset bank keeps the wide pixel reset tree away from final IOs.
 (* preserve="true",async_reg="true" *) reg output_reset_n;
 reg [23:0] output_flush;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin output_reset_n<=0;output_flush<=0;end else begin output_flush<={output_flush[22:0],1'b1};output_reset_n<=output_flush[23];end
 // Final composite register permits RGB/HS/VS packing into the original IOs.
 always @(posedge clk or negedge output_reset_n)
  if(!output_reset_n)begin vga_r<=0;vga_g<=0;vga_b<=0;vga_hs<=0;vga_vs<=0;final_x<=0;final_y<=0;final_de<=0;end
  else begin
   {vga_hs,vga_vs,final_de,final_x,final_y}<=overlay_pipe[14][45:24];
   {vga_r,vga_g,vga_b}<=overlay_pipe[14][43] ? ((native_active6 && page<4)?native_rgb6:overlay_pipe[14][23:0]) : 24'd0;
  end
 always @(posedge clk)begin
   overlay_pipe[0]<={overlay_hs,overlay_vs,de2,x2,y2,overlay_r,overlay_g,overlay_b};
   for(pipe_i=1;pipe_i<15;pipe_i=pipe_i+1)overlay_pipe[pipe_i]<=overlay_pipe[pipe_i-1];
  end
 always @(posedge clk)begin {overlay_r,overlay_g,overlay_b}<=de6?overlay_rgb6:24'd0;
   overlay_hs<=hs6;overlay_vs<=vs6;x2<=x6;y2<=y6;de2<=de6;end
endmodule
`default_nettype wire
