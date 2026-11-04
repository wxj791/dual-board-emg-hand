`timescale 1ns/1ps
`default_nettype none
// r49: one authoritative typed map. Display authority is independent of ARM.
module b_tasks_control #(
 parameter integer FRESH_CLKS=7200000, MIN_EVENT_CLKS=4800000,
 parameter integer COLLISION_SUPPORTED=0
)(
 input wire clk,rst_n,display_enabled,
 input wire command_valid,output wire command_ready,
 input wire [15:0] request_id,opcode,data_length,input wire [79:0] arguments,
 output reg response_valid,input wire response_ready,output reg [7:0] response_type,
 output reg [15:0] response_id,response_opcode,response_status,
 output reg [9:0] response_length,output reg [4095:0] response_data,
 input wire [9:0] response_byte_index,output wire [7:0] response_byte,
 input wire result_valid,wave_valid,link_fault,stale,
 input wire [15:0] generation,epoch,class_id,class_count,input wire [7:0] result_flags,
 input wire [31:0] window_seq,
 input wire [4:0] online,torque,input wire [8:0] pose_valid,
 input wire servo_idle,flash_ready,flash_available,flash_initialized,input wire [7:0] motion_state,
 input wire [79:0] positions,speeds,loads,feedback_age_ms,input wire [4:0] feedback_valid_mask,torque_valid_mask,
 input wire [4:0] active_mask,retired_mask,input wire [31:0] fault_flags,
 output reg req_valid,input wire req_ready,output reg [2:0] req_op,
 output reg [15:0] req_id,req_arg,
 input wire event_valid,output wire event_ready,input wire [15:0] event_id,event_status,
 output wire [3:0] query_pose,input wire query_valid,input wire [49:0] query_positions,
 output reg [2:0] page,output reg freeze,armed,teach_mode,
 output reg [127:0] ranges,gains,output reg [7:0] custom,
 output reg [15:0] revision,
 output reg [31:0] auto_count,auto_rejects
);
 // Class ID is the row address; reserved fields are validated before storage.
 // Store only legal kind/enabled/pose bits, reconstruct the canonical wire row.
 reg [7:0] map0[0:31],map1[0:31];
 reg active_bank,mapping_valid,stage_started;
 reg [15:0] active_generation,active_count,stage_generation,stage_count;
 reg [31:0] map_revision,stage_revision,written;
 reg [2:0] range_pages[0:31];reg [12:0] gain_pages[0:31];reg [31:0] custom_pages;
 reg committing;reg [7:0] crc_index;reg [31:0] crc,expected_crc;
 reg [15:0] commit_id;
 reg normal_pending,stop_pending;reg [15:0] normal_id,normal_opcode,stop_id,stop_opcode;
 reg [1:0] pose_query_wait;reg [15:0] pose_query_id;
 reg teach_pending,fault_stop_latched,fault_stop_needed;
 reg [2:0] json_slot;
 reg [79:0] json_loads,json_ages;
 reg [127:0] json_words0,json_words1;
 wire [79:0] json_positions=json_words0[79:0],json_speeds=json_words1[79:0];
 wire [127:0] json_ranges=json_words0,json_gains=json_words1;
 wire [1:0] json_query_page=arguments[1:0]-2'd1;
 reg [4:0] json_online,json_torque,json_valid,json_torque_valid;
 reg [7:0] json_custom;reg [31:0] json_faults;
 (* ram_style="bram_32k" *)reg [15:0] query_tokens[0:4095];reg [15:0] query_token;
 initial $readmemh("fpga_b/gesture_tasks/assets/query_tokens.hex",query_tokens);
 always @(posedge clk)query_token<=query_tokens[{json_slot,response_byte_index[8:0]}];
 reg [3:0] json_nibble;
 always @*begin
  json_nibble=0;
  case(query_token[11:8])
   1:json_nibble=json_positions[query_token[4:0]*4+:4];
   2:json_nibble=json_speeds[query_token[4:0]*4+:4];
   3:json_nibble=json_loads[query_token[4:0]*4+:4];
   4:json_nibble=json_ages[query_token[4:0]*4+:4];
   5:json_nibble=({3'd0,json_online} >> (query_token[0]*4));
   6:json_nibble=({3'd0,json_torque} >> (query_token[0]*4));
   7:json_nibble=({3'd0,json_valid} >> (query_token[0]*4));
   8:json_nibble=json_faults[query_token[2:0]*4+:4];
   9:json_nibble=json_ranges[query_token[4:0]*4+:4];
   10:json_nibble=json_gains[query_token[4:0]*4+:4];
   11:json_nibble=json_custom[query_token[0]*4+:4];
   12:json_nibble=({3'd0,json_torque_valid} >> (query_token[0]*4));
  endcase
 end
 assign response_byte=json_slot==7?response_data[response_byte_index[4:0]*8+:8]:
    query_token[11:8]==0?query_token[7:0]:hex_ascii(json_nibble);
 reg [31:0] result_age,event_age;reg result_seen,trigger_ready;
 reg [15:0] last_generation,last_epoch,last_class,last_class_count;reg [31:0] last_window;
 reg [1:0] stable_windows;reg [63:0] row;
 integer i;
 function [31:0] crc_byte;input [31:0] c;input [7:0] b;reg [31:0] v;integer k;
  begin v=c^b;for(k=0;k<8;k=k+1)v=v[0]?(v>>1)^32'hedb88320:v>>1;crc_byte=v;end
 endfunction
 function legal_range;input [15:0] r;
  begin legal_range=r==20 || r==50 || r==100 || r==200 || r==500 || r==1200 || r==3300;end
 endfunction
 function [2:0] range_code;input [15:0] r;
  begin case(r)20:range_code=0;50:range_code=1;100:range_code=2;200:range_code=3;
   500:range_code=4;1200:range_code=5;default:range_code=6;endcase end
 endfunction
 function [15:0] range_value;input [2:0] code;
  begin case(code)0:range_value=20;1:range_value=50;2:range_value=100;3:range_value=200;
   4:range_value=500;5:range_value=1200;default:range_value=3300;endcase end
 endfunction
 function [2:0] zoom_code;input [2:0] code;input inward;
  begin zoom_code=inward?(code==0?0:code-1'b1):(code>=6?6:code+1'b1);end
 endfunction
 function [7:0] hex_ascii;input [3:0] n;
  begin hex_ascii=n<10?8'd48+n:8'd87+n;end
 endfunction
 function [63:0] expand_row;input [7:0] entry;input [4:0] cls;
  begin expand_row={16'd0,12'd0,entry[7:4],7'd0,entry[3],5'd0,entry[2:0],11'd0,cls};end
 endfunction
 function [15:0] zoom;input [15:0] r;input inward;
  begin if(inward)case(r)20,50:zoom=20;100:zoom=50;200:zoom=100;500:zoom=200;1200:zoom=500;default:zoom=1200;endcase
  else case(r)20:zoom=50;50:zoom=100;100:zoom=200;200:zoom=500;500:zoom=1200;default:zoom=3300;endcase end
 endfunction
 function legal_row;input [63:0] r;
  begin legal_row=r[15:0]<stage_count && r[23:16]<=6 && (r[23:16]!=2 || COLLISION_SUPPORTED) && r[31:24]<=1 && r[63:48]==0 &&
   (r[23:16]==1 ? r[47:32]<9 : r[47:32]==0) &&
   (r[15:0]!=0 || (r[23:16]==0 && r[31:24]==0));end
 endfunction
 wire [63:0] stage_row=expand_row(active_bank?map0[crc_index[7:3]]:map1[crc_index[7:3]],crc_index[7:3]);
 wire [31:0] next_crc=crc_byte(crc,stage_row[crc_index[2:0]*8+:8]);
 wire [31:0] row_mask=32'hffffffff>>(32-stage_count);
 wire model_matches=mapping_valid && active_generation==generation && active_count==class_count;
 wire valid_result=result_flags==3 && class_id<class_count && class_count>=2 && class_count<=32;
 wire can_reply=!response_valid;
 assign command_ready=can_reply && pose_query_wait==0 && (!committing || opcode==16'h0300);
 assign event_ready=can_reply && !command_valid && !committing && pose_query_wait==0;
 assign query_pose=arguments[3:0];
 reg [15:0] summary_range;
 always @*begin
  row=0;if(class_id<32)row=expand_row(active_bank?map1[class_id[4:0]]:map0[class_id[4:0]],class_id[4:0]);
  summary_range=ranges[15:0];
  for(integer n=0;n<8;n=n+1)if(ranges[n*16+:16]!=ranges[15:0] || custom[n])summary_range=16'hffff;
 end
 task reply;input [15:0] s;begin
  response_valid<=1;response_type<=8'h51;response_id<=request_id;response_opcode<=opcode;
  response_status<=s;response_length<=0;response_data<=0;
  json_slot<=7;
 end endtask
 task change_page;input [2:0] p;begin
  page<=p;revision<=revision+1'b1;
  for(integer n=0;n<8;n=n+1)begin
   ranges[n*16+:16]<=range_value(range_pages[p*8+n]);gains[n*16+:16]<=gain_pages[p*8+n];custom[n]<=custom_pages[p*8+n];
  end
 end endtask
 task issue;input [2:0] op;input [15:0] arg;input [15:0] id;begin
  req_valid<=1;req_op<=op;req_arg<=arg;req_id<=id;
 end endtask
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   response_valid<=0;response_type<=0;response_id<=0;response_opcode<=0;response_status<=0;response_length<=0;response_data<=0;
   json_slot<=7;json_words0<=0;json_words1<=0;json_loads<=0;json_ages<=0;
   json_online<=0;json_torque<=0;json_valid<=0;json_torque_valid<=0;json_custom<=0;json_faults<=0;
   req_valid<=0;req_op<=0;req_id<=0;req_arg<=0;
   page<=0;freeze<=0;armed<=0;teach_mode<=0;teach_pending<=0;fault_stop_latched<=0;fault_stop_needed<=0;revision<=0;
   ranges<={8{16'd1200}};gains<={8{16'd256}};custom<=0;custom_pages<=0;
   active_bank<=0;mapping_valid<=0;stage_started<=0;active_generation<=0;active_count<=0;
   stage_generation<=0;stage_count<=0;stage_revision<=0;map_revision<=0;written<=0;
   committing<=0;crc_index<=0;crc<=0;expected_crc<=0;commit_id<=0;
   normal_pending<=0;normal_id<=0;normal_opcode<=0;stop_pending<=0;stop_id<=0;stop_opcode<=0;
   pose_query_wait<=0;pose_query_id<=0;
   result_age<=FRESH_CLKS;result_seen<=0;trigger_ready<=0;last_generation<=0;last_epoch<=0;
   last_class<=16'hffff;last_class_count<=0;last_window<=0;stable_windows<=0;event_age<=MIN_EVENT_CLKS;
   auto_count<=0;auto_rejects<=0;
   for(i=0;i<32;i=i+1)begin range_pages[i]<=i<8?5:i<16?3:2;gain_pages[i]<=256;end
  end else begin
   if(response_valid && response_ready)response_valid<=0;
   if(pose_query_wait!=0)begin
    pose_query_wait<=pose_query_wait-1'b1;
    if(pose_query_wait==1)begin
     response_valid<=1;response_type<=8'h51;response_id<=pose_query_id;response_opcode<=16'h030a;
     response_status<=0;response_length<=13;json_slot<=7;response_data<=
       {{7'd0,flash_initialized},{7'd0,flash_available},
        6'd0,query_positions[49:40],6'd0,query_positions[39:30],6'd0,query_positions[29:20],
        6'd0,query_positions[19:10],6'd0,query_positions[9:0],{7'd0,query_valid}};
    end
   end
   if(req_valid && req_ready)req_valid<=0;
   if(result_age<FRESH_CLKS)result_age<=result_age+1'b1;
   if(event_age<MIN_EVENT_CLKS)event_age<=event_age+1'b1;
   if(motion_state!=1 && motion_state!=2)fault_stop_latched<=0;
   // A model/link revocation can coincide with acceptance of a queued start.
   // Remember it until the bus request is consumed, then send one priority STOP.
   if(fault_stop_needed && !req_valid && !stop_pending)begin
    issue(3,0,16'hfffe);fault_stop_needed<=0;fault_stop_latched<=1;
   end
   if(link_fault || stale || (result_valid && !valid_result) || result_age==FRESH_CLKS-1 ||
      (result_seen && (generation!=last_generation || epoch!=last_epoch || class_count!=last_class_count)))begin
    armed<=0;trigger_ready<=0;stable_windows<=0;
    if((motion_state==1 || motion_state==2) && !fault_stop_latched && !req_valid && !stop_pending)begin
     issue(3,0,16'hfffe);fault_stop_latched<=1;
    end
    if(req_valid && (req_op==2 || req_op==4))fault_stop_needed<=1;
   end
   if(result_valid)begin
    result_age<=0;result_seen<=valid_result;last_generation<=generation;last_epoch<=epoch;last_class_count<=class_count;
    if(!valid_result)begin armed<=0;trigger_ready<=0;stable_windows<=0;end
    else if(!result_seen || generation!=last_generation || epoch!=last_epoch || class_count!=last_class_count)begin
     armed<=0;trigger_ready<=0;stable_windows<=0;last_class<=class_id;last_window<=window_seq;
    end else if(window_seq!=last_window)begin
     last_window<=window_seq;last_class<=class_id;
     if(class_id==0)begin trigger_ready<=1;stable_windows<=0;end
     else if(class_id!=last_class)stable_windows<=1;
     else if(stable_windows<2)stable_windows<=stable_windows+1'b1;
     // Two distinct stable windows, >=200ms between events, rest re-arms.
     if(class_id!=0 && class_id==last_class && stable_windows>=1 && trigger_ready && event_age>=MIN_EVENT_CLKS)begin
      trigger_ready<=0;event_age<=0;
      if(model_matches && row[31:24]==1)begin
       if(row[23:16]>=3 && row[23:16]<=6)begin
        if(display_enabled)begin
         if(row[23:16]==3)change_page(page==0?3:page-1'b1);
         else if(row[23:16]==4)change_page(page==3?0:page+1'b1);
         else begin
          for(i=0;i<8;i=i+1)begin
           ranges[i*16+:16]<=range_value(zoom_code(range_pages[page*8+i],row[23:16]==5));
           range_pages[page*8+i]<=zoom_code(range_pages[page*8+i],row[23:16]==5);
           custom_pages[page*8+i]<=0;
          end
          custom<=0;revision<=revision+1'b1;
         end
         auto_count<=auto_count+1'b1;
        end else auto_rejects<=auto_rejects+1'b1;
       end else if(row[23:16]==1 || row[23:16]==2)begin
        if(armed && !teach_mode && !teach_pending && servo_idle && !req_valid && !normal_pending &&
           online==31 && (row[23:16]!=1 || pose_valid[row[35:32]]) &&
           (row[23:16]!=2 || COLLISION_SUPPORTED))begin
         issue(row[23:16]==1?2:4,row[47:32],16'hffff);auto_count<=auto_count+1'b1;
        end else auto_rejects<=auto_rejects+1'b1;
       end
      end else auto_rejects<=auto_rejects+1'b1;
     end
    end
   end
   if(event_valid && event_ready)begin
    if(normal_pending && event_id==normal_id)begin
     response_valid<=1;response_type<=8'h52;response_id<=normal_id;response_opcode<=normal_opcode;
     response_status<=event_status;response_length<=0;response_data<=0;normal_pending<=0;
     json_slot<=7;
    end else if(stop_pending && event_id==stop_id)begin
     response_valid<=1;response_type<=8'h52;response_id<=stop_id;response_opcode<=stop_opcode;
     response_status<=event_status;response_length<=0;response_data<=0;stop_pending<=0;
     json_slot<=7;
     if(teach_pending)begin teach_pending<=0;teach_mode<=event_status==0;end
    end
   end
   if(committing)begin
    crc<=next_crc;crc_index<=crc_index+1'b1;
    if({1'b0,crc_index}+9'd1=={stage_count[5:0],3'd0})begin
     committing<=0;response_valid<=1;response_type<=8'h51;response_id<=commit_id;
     response_opcode<=16'h0602;response_length<=0;response_data<=0;
     if((next_crc^32'hffffffff)==expected_crc && generation==stage_generation && class_count==stage_count)begin
      active_bank<=!active_bank;mapping_valid<=1;active_generation<=stage_generation;active_count<=stage_count;
      map_revision<=stage_revision;stage_started<=0;armed<=0;trigger_ready<=0;response_status<=0;
     end else response_status<=8;
    end
   end
   if(command_valid && command_ready)begin
    reply(0);
    case(opcode)
     16'h0001:begin
      if(data_length!=0)reply(3);
      else begin
       // Generated constants are byte-packed little-endian, never empty JSON.
       `include "fpga_b/gesture_tasks/caps.vh"
      end
     end
     16'h0002:begin
      if(data_length>1 || (data_length==1 && arguments[7:0]>4))reply(3);
      else begin
       `include "fpga_b/gesture_tasks/status.vh"
      end
     end
     16'h0200:if(data_length!=1 || arguments[7:0]>3)reply(3);else change_page(arguments[2:0]);
     16'h0201:if(data_length!=1 || (arguments[7:0]!=1 && arguments[7:0]!=255))reply(3);
      else change_page(arguments[7:0]==1?(page==3?0:page+1'b1):(page==0?3:page-1'b1));
     16'h0202:if(data_length!=1 || arguments[7:0]>1)reply(3);
      else begin freeze<=arguments[0];if(freeze && !arguments[0])revision<=revision+1'b1;end
     16'h0203,16'h0204:begin
      if(data_length!=4 || arguments[7:0]>3 || (arguments[15:8]>7 && arguments[15:8]!=255) ||
       (opcode==16'h0204 ? !legal_range(arguments[31:16]) : (arguments[31:16]==0 || arguments[31:16]>4096)))reply(3);
      else begin
       for(i=0;i<8;i=i+1)if(arguments[15:8]==255 || arguments[15:8]==i)begin
        custom_pages[arguments[1:0]*8+i]<=opcode==16'h0203;
        if(opcode==16'h0204)range_pages[arguments[1:0]*8+i]<=range_code(arguments[31:16]);
        else gain_pages[arguments[1:0]*8+i]<=arguments[28:16];
        if(page==arguments[2:0])begin
         custom[i]<=opcode==16'h0203;
         if(opcode==16'h0204)ranges[i*16+:16]<=arguments[31:16];else gains[i*16+:16]<=arguments[31:16];
        end
       end
       if(page==arguments[2:0])revision<=revision+1'b1;
      end
     end
     16'h0600:begin
      if(data_length!=8 || arguments[31:16]<2 || arguments[31:16]>32)reply(3);
      else if(arguments[15:0]!=generation || arguments[31:16]!=class_count || !result_seen)reply(8);
      else begin stage_generation<=arguments[15:0];stage_count<=arguments[31:16];stage_revision<=arguments[63:32];
       written<=0;stage_started<=1;end
     end
     16'h0601:begin
      if(!stage_started)reply(5);
      else if(data_length!=8 || !legal_row(arguments[63:0]))reply(3);
      else begin
       if(active_bank)map0[arguments[4:0]]<={arguments[35:32],arguments[24],arguments[18:16]};
       else map1[arguments[4:0]]<={arguments[35:32],arguments[24],arguments[18:16]};
       written[arguments[4:0]]<=1;
      end
     end
     16'h0602:begin
      if(data_length!=8)reply(3);
      else if(!servo_idle || req_valid || normal_pending)reply(4);
      else if(!stage_started || written!=row_mask || arguments[31:0]!=stage_revision ||
       generation!=stage_generation || class_count!=stage_count)reply(8);
      else begin committing<=1;crc_index<=0;crc<=32'hffffffff;expected_crc<=arguments[63:32];
       commit_id<=request_id;response_valid<=0;end
     end
     16'h0603:begin
      if(data_length!=2 || arguments[15:0]>=active_count)reply(3);
      else if(!mapping_valid)reply(5);
      else begin response_data<=expand_row(active_bank?map1[arguments[4:0]]:map0[arguments[4:0]],arguments[4:0]);response_length<=8;end
     end
     16'h0604:begin
      if(data_length!=0)reply(3);
      else begin response_length<=28;response_data<=
       {fault_flags,16'd0,summary_range,{5'd0,page},{7'd0,flash_ready},{7'd0,pose_valid},
        {3'd0,retired_mask},{3'd0,active_mask},motion_state,{7'd0,model_matches},map_revision,
        active_count,active_generation,{7'd0,teach_mode},{7'd0,armed},{7'd0,display_enabled},8'd1};end
     end
     16'h0300,16'h0605:begin
      if((opcode==16'h0300 && data_length!=0) ||
         (opcode==16'h0605 && (data_length!=1 || arguments[7:0]>1)))reply(3);
      else if(opcode==16'h0605 && !arguments[0])begin
       if(stop_pending || !servo_idle)reply(4);else begin teach_mode<=0;armed<=0;trigger_ready<=0;end
      end else if(stop_pending)reply(4);
      else begin
       committing<=0;armed<=0;trigger_ready<=0;teach_mode<=0;teach_pending<=opcode==16'h0605;
       stop_pending<=1;stop_id<=request_id;stop_opcode<=opcode;issue(3,0,request_id);reply(1);
      end
     end
     16'h0301:begin
      if(data_length!=2)reply(3);
      else if(!model_matches || arguments[15:0]!=generation || !valid_result || !result_seen ||
        stale || result_age>=FRESH_CLKS || online!=31 || !servo_idle || teach_mode || teach_pending)reply(5);
      else begin armed<=1;trigger_ready<=0;end
     end
     16'h0302:if(data_length!=0)reply(3);else begin armed<=0;trigger_ready<=0;end
     16'h0304,16'h0305,16'h0606:begin
      if(opcode==16'h0606 && !COLLISION_SUPPORTED)reply(6);
      else if((opcode==16'h0606 && data_length!=0) || (opcode!=16'h0606 && (data_length!=2 || arguments[15:0]>=9)))reply(3);
      else if(normal_pending || stop_pending || req_valid || !servo_idle)reply(4);
      else if(online!=31 || (opcode==16'h0304 && (!teach_mode || !flash_ready || torque!=0)) ||
       (opcode==16'h0305 && (!pose_valid[arguments[3:0]] || teach_mode)) ||
       (opcode==16'h0606 && (!armed || teach_mode)))reply(5);
      else begin normal_pending<=1;normal_id<=request_id;normal_opcode<=opcode;
       issue(opcode==16'h0304 ? 1 : opcode==16'h0305 ? 2 : 4,arguments[15:0],request_id);reply(1);end
     end
     16'h030a:begin
      if(data_length!=2 || arguments[15:0]>=9)reply(3);
      else begin response_valid<=0;pose_query_wait<=2;pose_query_id<=request_id;end
     end
     default:reply(6);
    endcase
   end
  end
 end
endmodule
`default_nettype wire
