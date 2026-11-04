`default_nettype none
// Bounded binary commands. Canonical metadata is validated byte-for-byte.
// In-place transaction: BEGIN disables inference, any failure needs full reload.
module v2_pc #(parameter integer CAPACITY=7,parameter integer BIT_CYCLES=26,
 parameter [31:0] FACTORY_CRC32=32'hfc91830a)(
 input wire clk,input wire rst_n,input wire rx,output wire tx,
 input wire pipeline_idle,input wire [31:0] timestamp,
 input wire [31:0] accepted,wave_drops,results,wireless_bad,input wire [511:0] diagnostic_snapshot,
 output reg source_pc,output reg [15:0] telemetry_div,
 output reg model_valid,output reg loading,output reg clear_pipeline,
 output reg [15:0] generation,active_classes,
 output reg parameter_write,output reg [2:0] parameter_bank,
 output reg [31:0] parameter_address,parameter_data,
 output reg replay_valid,input wire replay_ready,output reg [127:0] replay_data,
 output reg [31:0] command_errors,
 output reg forward_start,output wire forward_stop,output wire [6:0] forward_length,
 input wire forward_ready,output reg forward_valid,input wire forward_take,output wire [7:0] forward_data,
 input wire b_rx,input wire [255:0] bridge_diagnostic,
 input wire stream_valid,output wire stream_ready,input wire [7:0] stream_type,input wire [10:0] stream_length,
 input wire stream_payload_valid,output wire stream_payload_ready,input wire [7:0] stream_data
);
 wire [7:0] rb;wire bv,fe;
 ingress_uart_rx #(.CLKS_PER_BIT(BIT_CYCLES)) uart_rx(clk,rst_n,rx,rb,bv,fe,/*busy*/);
 wire fv,pv,pl;wire [7:0] ft,pd;wire [10:0] fl;
 wire frame_ready,payload_ready;
 wire [31:0] crc_errors;
 link_frame_rx receiver(.clk(clk),.rst_n(rst_n),.byte_valid(bv),.byte_ready(),.byte_data(rb),
  .frame_valid(fv),.frame_ready(frame_ready),.frame_type(ft),.frame_seq(),.frame_timestamp(),
  .frame_length(fl),.payload_valid(pv),.payload_ready(payload_ready),.payload_data(pd),.payload_last(pl),
  .good_frame_pulse(),.good_frame_type(),.busy(),.state_code(),.good_frame_count(),.byte_count(),
  .crc_error_count(crc_errors),.sequence_gap_count(),.length_error_count(),.version_error_count(),
  .timeout_count(),.selftest_error_count(),.last_error_code());
 (* ram_style="bram" *) reg [7:0] command[0:1023];
 reg [7:0] cq;reg [9:0] ca,write_index;
 always @(posedge clk) begin
  cq<=command[ca];
  if(pv && payload_ready) command[write_index]<=pd;
 end
 // The command header is captured while draining the validated frame.
 reg [63:0] header;reg [10:0] length;
 reg queued_while_busy,last_request_valid;reg [15:0] last_request_id;
 reg [7:0] state;
 reg [7:0] response_target;
 localparam IDLE=0,RECEIVE=1,DISPATCH=2,ADDR=3,READ=4,USE=5,RESPOND=6,
  VALIDATE_META=7,WRITE_WORD=8,REPLAY_NEXT=9,WAIT_REPLAY=10,CRC_BYTES=11;
 reg [7:0] after_read;
 reg [15:0] status,request_id,opcode,data_length;
 reg [31:0] scalar;reg [2:0] scalar_index;
 reg [9:0] cursor;
 // Latch only verified model identity; model_valid qualifies it.
 reg [31:0] model_crc32,s_model_crc32;
 reg [31:0] crc32;
 reg [71:0] crc_shift;
 reg [3:0] crc_left;
 reg [7:0] after_crc;
 reg [2:0] next_bank;
 localparam integer CLASS_COUNT_BITS=$clog2(CAPACITY+1);
 localparam integer PARAMETER_ADDRESS_BITS=$clog2((CAPACITY*304>1280 ? CAPACITY*304 : 1280)+1);
 // Validated ranges bound these internal counters. Incoming request fields
 // remain full width, so an out-of-range address/count is still rejected.
 reg [PARAMETER_ADDRESS_BITS-1:0] next_address;
 wire [31:0] canonical_address=next_address;
 reg [CLASS_COUNT_BITS-1:0] new_classes;
 reg [15:0] word_count;reg [7:0] word_left;
 reg [31:0] write_address;
 reg [7:0] write_bank;
 reg [31:0] word_value;
 reg [2:0] field_index;
 reg [15:0] replay_count;reg [4:0] replay_left;
 reg [2:0] replay_channel;
 reg [127:0] replay_hold;
 reg [10:0] response_length;

 wire [7:0] response_q;reg [8:0] response_address;
 reg [8:0] response_override_q;
 reg [15:0] response_template_q;
 wire [8:0] response_template_address=response_address-9'd10+
  (json_kind==1 ? 9'd0 : json_kind==2 ? 9'd114 : json_kind==4 ? 9'd474 : 9'd431);
 (* ram_style="bram" *) reg [15:0] response_template[0:511];
 integer template_index;
 initial begin
  for(template_index=0;template_index<512;template_index=template_index+1)
   response_template[template_index]={8'b0,RESPONSE_TEMPLATE[(511-template_index)*8 +:8]};
  response_template[144]=16'h4000;
  response_template[156]=16'h4001;
  response_template[170]=16'h4002;
  response_template[186]=16'h4006;
  response_template[187]=16'h4005;
  response_template[188]=16'h4004;
  response_template[189]=16'h4003;
  response_template[210]=16'h400a;
  response_template[211]=16'h4009;
  response_template[212]=16'h4008;
  response_template[213]=16'h4007;
  response_template[232]=16'h4012;
  response_template[233]=16'h4011;
  response_template[234]=16'h4010;
  response_template[235]=16'h400f;
  response_template[236]=16'h400e;
  response_template[237]=16'h400d;
  response_template[238]=16'h400c;
  response_template[239]=16'h400b;
  response_template[255]=16'h401a;
  response_template[256]=16'h4019;
  response_template[257]=16'h4018;
  response_template[258]=16'h4017;
  response_template[259]=16'h4016;
  response_template[260]=16'h4015;
  response_template[261]=16'h4014;
  response_template[262]=16'h4013;
  response_template[280]=16'h4022;
  response_template[281]=16'h4021;
  response_template[282]=16'h4020;
  response_template[283]=16'h401f;
  response_template[284]=16'h401e;
  response_template[285]=16'h401d;
  response_template[286]=16'h401c;
  response_template[287]=16'h401b;
  response_template[310]=16'h402a;
  response_template[311]=16'h4029;
  response_template[312]=16'h4028;
  response_template[313]=16'h4027;
  response_template[314]=16'h4026;
  response_template[315]=16'h4025;
  response_template[316]=16'h4024;
  response_template[317]=16'h4023;
  response_template[342]=16'h4032;
  response_template[343]=16'h4031;
  response_template[344]=16'h4030;
  response_template[345]=16'h402f;
  response_template[346]=16'h402e;
  response_template[347]=16'h402d;
  response_template[348]=16'h402c;
  response_template[349]=16'h402b;
  response_template[370]=16'h403a;
  response_template[371]=16'h4039;
  response_template[372]=16'h4038;
  response_template[373]=16'h4037;
  response_template[374]=16'h4036;
  response_template[375]=16'h4035;
  response_template[376]=16'h4034;
  response_template[377]=16'h4033;
  response_template[399]=16'h4042;
  response_template[400]=16'h4041;
  response_template[401]=16'h4040;
  response_template[402]=16'h403f;
  response_template[403]=16'h403e;
  response_template[404]=16'h403d;
  response_template[405]=16'h403c;
  response_template[406]=16'h403b;
  response_template[429]=16'h4000;
 end
 wire [267:0] status_nibbles={s_model_crc32,s_crc_errors,s_command_errors,s_wireless_bad,
  s_results,s_wave_drops,s_accepted,s_generation,s_active_classes,3'b0,s_source,3'b0,s_loading,3'b0,s_valid};
 reg [15:0] diagnostic_token;
 wire [15:0] selected_token=diagnostic_token[15] ? diagnostic_token : response_template_q;
 wire [3:0] selected_nibble=selected_token[15:14]==1 ? status_nibbles[selected_token[6:0]*4 +:4] :
  selected_token[15:14]==2 ? s_diagnostic[selected_token[6:0]*4 +:4] :
  bridge_snapshot[selected_token[5:0]*4 +:4];
 always @(posedge clk) begin
  diagnostic_token<=0;
  if(json_kind==3 && response_address>=53 && response_address<181)
   diagnostic_token<=16'h8000+(180-response_address);
  else if(json_kind==4 && response_address>=28 && response_address<92)
   diagnostic_token<=16'hc000+(91-response_address);
  response_template_q<=response_template[response_template_address];
  response_override_q<=response_byte(response_address);
 end
 wire [7:0] template_byte=selected_token[15:14]==0 ? response_template_q[7:0] : hexchar(selected_nibble);
 assign response_q=response_override_q[8] ? response_override_q[7:0] : template_byte;
 // One bounded, canonical JSON schema. Spaces before decimal count are legal JSON.
 localparam integer META_LEN=99;
 localparam [META_LEN*8-1:0] META="{\"format\":\"EMG-A-AB-V2-1\",\"classes\":    7,\"architecture\":\"clean-cnn256-stat304-q23-q8\",\"stride\":32}";
 localparam [7:0] CAP_HUNDREDS=CAPACITY>=100 ? 48+CAPACITY/100 : 32;
 localparam [7:0] CAP_TENS=CAPACITY>=10 ? 48+(CAPACITY/10)%10 : 32;
 localparam [7:0] CAP_UNITS=48+CAPACITY%10;
 localparam [911:0] PACKED_CAPS={"{\"identity\":\"A-AB-V2\",\"protocol\":1,\"capacity\":",CAP_HUNDREDS,CAP_TENS,CAP_UNITS,",\"trained_classes\":7,\"flash\":false,\"wave\":64,\"classification\":65}"};
 function [7:0] hexchar;
  input [3:0] v;begin hexchar=v<10 ? 8'd48+v : 8'd87+v;end
 endfunction
 function [63:0] hex32;
  input [31:0] v;integer k;begin for(k=0;k<8;k=k+1) hex32[(7-k)*8 +:8]=hexchar(v[(7-k)*4 +:4]);end
 endfunction
 function [31:0] hex16;
  input [15:0] v;integer k;begin for(k=0;k<4;k=k+1) hex16[(3-k)*8 +:8]=hexchar(v[(3-k)*4 +:4]);end
 endfunction
 reg [7:0] meta_index;reg meta_bad,seen_digit;
 // Canonical metadata has exactly five decimal positions: 0..99999 needs
 // 17 bits. Keep the high bit so values such as 65538 cannot wrap to two.
 reg [16:0] parsed_count;
 function [7:0] meta_byte;
  input [7:0] n;begin meta_byte=META[(META_LEN-1-n)*8 +:8];end
 endfunction
 reg [511:0] s_diagnostic;reg [255:0] bridge_snapshot;
 reg s_valid,s_loading,s_source;
 reg [15:0] s_active_classes,s_generation;
 reg [31:0] s_accepted,s_wave_drops,s_results,s_wireless_bad,s_command_errors,s_crc_errors;
 localparam [2535:0] STATUS_TEMPLATE="{\"identity\":\"A-AB-V2\",\"valid\":0,\"loading\":0,\"source_pc\":0,\"active_hex\":\"0000\",\"generation_hex\":\"0000\",\"accepted_hex\":\"00000000\",\"drops_hex\":\"00000000\",\"results_hex\":\"00000000\",\"wireless_bad_hex\":\"00000000\",\"command_errors_hex\":\"00000000\",\"crc_errors_hex\":\"00000000\",\"model_crc32_hex\":\"00000000\",\"model_crc32_valid\":0}";
 localparam [143:0] BRIDGE_PREFIX="{\"bridge_v1_hex\":\"";
 localparam [343:0] DIAG_PREFIX="{\"identity\":\"A-AB-V2\",\"diagnostic_v6_hex\":\"";
 // Constant response text lives in one synchronous ROM, instead of LUT muxes.
 localparam [4095:0] RESPONSE_TEMPLATE={PACKED_CAPS,STATUS_TEMPLATE,DIAG_PREFIX,BRIDGE_PREFIX,160'b0};
 function [8:0] response_byte;
  input [8:0] a;integer n;
  begin
   n=a-10;response_byte=0;
   case(a)
    0:response_byte={1'b1,8'd1};1:response_byte={1'b1,response_target};
    2:response_byte={1'b1,request_id[7:0]};3:response_byte={1'b1,request_id[15:8]};
    4:response_byte={1'b1,opcode[7:0]};5:response_byte={1'b1,opcode[15:8]};
    6:response_byte={1'b1,status[7:0]};7:response_byte={1'b1,status[15:8]};
    8:response_byte={1'b1,json_length[7:0]};9:response_byte={1'b1,5'b0,json_length[10:8]};
    default:begin
     if(json_kind==4 && n>=82) response_byte={1'b1,(n==82 ? 8'h22 : 8'h7d)};
     else if(json_kind==3 && n>=171) response_byte={1'b1,(n==171 ? 8'h22 : 8'h7d)};
    end
   endcase
  end
 endfunction
 function [31:0] crc_byte;
  input [31:0] c;input [7:0] b;reg [31:0] v;integer j;
  begin v=c^b;for(j=0;j<8;j=j+1) v=v[0] ? (v>>1)^32'hedb88320 : v>>1;crc_byte=v;end
 endfunction
 // One byte datapath is shared by metadata and parameter records. UART
 // reception is buffered, so these nine clocks never affect sample processing.
 wire [31:0] crc_next=crc_byte(crc32,state==CRC_BYTES ? crc_shift[7:0] : cq);
 function [31:0] bank_size;
  input [2:0] bank;
  begin case(bank)
   0:bank_size=({new_classes,8'b0}+{new_classes,5'b0}+{new_classes,4'b0});1:bank_size=304;2:bank_size=new_classes*2;
   3:bank_size=1280;4:bank_size=160;5:bank_size=new_classes*32;
   6:bank_size=new_classes*2;7:bank_size=new_classes;
  endcase end
 endfunction
 function [15:0] mv;
  input [31:0] x;reg [23:0] sig;reg [11:0] whole;reg increment;reg [12:0] rounded;
  begin
   sig={1'b1,x[22:0]};whole=0;increment=0;rounded=0;
   if(x[31] || x[30:23]<126) mv=0;
   else if(x[30:23]>=139) mv=3300;
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
    mv=rounded>3300 ? 16'd3300 : {3'b0,rounded};
   end
  end
 endfunction
 assign frame_ready=state==IDLE;
 assign payload_ready=state==RECEIVE;
 // Response frame encoder feeds a byte UART. No status bytes share wireless RX.
 wire tfv,tfr,tpv,tpr,phy_valid;
 wire bfv,bfr,bpv,bpr;wire [7:0] bft,bpd;wire [10:0] bfl;
 wire [31:0] berrors;
 wire bevent,bfinal;wire [15:0] bid,bop;
 gt_return #(.BIT_CYCLES(BIT_CYCLES)) ret(clk,rst_n,b_rx,bfv,bfr,bft,bfl,bpv,bpr,bpd,berrors,bevent,bid,bop,bfinal);
 reg motion_pending;reg [15:0] motion_id,motion_opcode;
 wire motion_request=header[47:32]==16'h301 || header[47:32]==16'h303 || header[47:32]==16'h304 ||
  header[47:32]==16'h305 || header[47:32]==16'h306 || header[47:32]==16'h605 || header[47:32]==16'h606;

 reg response_b,last_b,response_stream,last_stream;
 wire choose_stream=stream_valid && ((!bfv && !tfv) || !last_stream);
 wire choose_b=bfv && (tx_state==0 || !last_b);
 wire encoder_fv=response_stream ? stream_valid : (response_b ? bfv : tfv);
 wire selected_payload_valid=response_stream ? stream_payload_valid : (response_b ? bpv : tpv);
 assign stream_ready=encoder_selected && response_stream && tfr;
 assign stream_payload_ready=response_stream && tpr;
 assign bfr=encoder_selected && !response_stream && response_b && tfr;
 assign bpr=!response_stream && response_b && tpr;
 wire local_tfr=encoder_selected && !response_stream && !response_b && tfr,local_tpr=!response_stream && !response_b && tpr;
 assign forward_stop=header[47:32]==16'h0300;
 assign forward_length=length[6:0];
 assign forward_data=cq;
wire [7:0] phy_byte;
 wire byte_tick;
 reg [9:0] tx_shift;reg [3:0] bits_left;reg [7:0] bit_timer;
 assign tx=bits_left==0 ? 1'b1 : tx_shift[0];
 assign byte_tick=bits_left==0;
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin tx_shift<=1023;bits_left<=0;bit_timer<=0;end
  else if(bits_left!=0) begin
   if(bit_timer==0) begin tx_shift<={1'b1,tx_shift[9:1]};bits_left<=bits_left-1'b1;bit_timer<=BIT_CYCLES-1;end
   else bit_timer<=bit_timer-1'b1;
  end else if(phy_valid) begin tx_shift<={1'b1,phy_byte,1'b0};bits_left<=10;bit_timer<=BIT_CYCLES-1;end
 end
 reg [2:0] tx_state;reg [10:0] tx_index;
 assign tfv=tx_state==1;
 assign tpv=tx_state==3;
 // Synchronous response memory, prefetch the subsequent byte on each take.
 always @* response_address=(tx_state==3 && local_tpr && tx_index<response_length-1) ? tx_index+1'b1 : tx_index;
 link_frame_tx encoder(.clk(clk),.rst_n(rst_n),.link_fall_tick(byte_tick),.phy_ready(1'b1),
  .frame_valid(encoder_fv && encoder_selected),.frame_ready(tfr),.frame_type(response_stream ? stream_type : (response_b ? bft : 8'h51)),.frame_timestamp(timestamp),.frame_length(response_stream ? stream_length : (response_b ? bfl : response_length)),
  .payload_valid(selected_payload_valid),.payload_ready(tpr),.payload_data(response_stream ? stream_data : (response_b ? bpd : response_q)),.phy_data(phy_byte),.phy_valid(phy_valid),
  .busy(),.state_code(),.next_sequence(),.frame_count(),.byte_count(),.max_stall_periods());
 reg encoder_selected,encoder_payload;reg [10:0] encoder_left;
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin response_b<=0;last_b<=1;response_stream<=0;last_stream<=1;encoder_selected<=0;encoder_payload<=0;encoder_left<=0;end
  else if(!encoder_selected) begin
   if(bfv || tfv || stream_valid) begin response_stream<=choose_stream;response_b<=!choose_stream && choose_b;encoder_selected<=1;end
  end else if(!encoder_payload) begin
   if(encoder_fv && tfr) begin encoder_payload<=1;encoder_left<=response_stream ? stream_length : (response_b ? bfl : response_length);end
  end else if(selected_payload_valid && tpr) begin
   encoder_left<=encoder_left-1'b1;
   if(encoder_left==1) begin encoder_selected<=0;encoder_payload<=0;if(!response_stream) last_b<=response_b;last_stream<=response_stream;end
  end
 end
 reg [15:0] json_index;reg [7:0] json_kind;

 reg [10:0] json_length;
 integer i;
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   motion_pending<=0;motion_id<=0;motion_opcode<=0;forward_start<=0;forward_valid<=0;response_target<=1;bridge_snapshot<=0;
   queued_while_busy<=0;last_request_valid<=0;last_request_id<=0;
   state<=IDLE;write_index<=0;header<=0;length<=0;ca<=0;cursor<=0;after_read<=0;
   source_pc<=0;telemetry_div<=1;model_valid<=1;loading<=0;clear_pipeline<=0;
   generation<=0;active_classes<=7;model_crc32<=FACTORY_CRC32;s_model_crc32<=0;parameter_write<=0;parameter_bank<=0;
   parameter_address<=0;parameter_data<=0;replay_valid<=0;replay_data<=0;command_errors<=0;
   status<=0;request_id<=0;opcode<=0;data_length<=0;scalar<=0;scalar_index<=0;
   crc32<=32'hffffffff;crc_shift<=0;crc_left<=0;after_crc<=0;
   next_bank<=0;next_address<=0;new_classes<=7;word_count<=0;word_left<=0;
   write_address<=0;write_bank<=0;word_value<=0;field_index<=0;
   replay_count<=0;replay_left<=0;replay_channel<=0;replay_hold<=0;
   meta_index<=0;meta_bad<=0;seen_digit<=0;parsed_count<=0;
   response_length<=10;tx_state<=0;tx_index<=0;json_index<=0;json_kind<=0;json_length<=0;
  end else begin
   clear_pipeline<=0;parameter_write<=0;forward_start<=0;forward_valid<=0;
   if(bevent && bfinal && bid==motion_id && bop==motion_opcode) motion_pending<=0;
   if(fv && state!=IDLE) queued_while_busy<=1;
   case(tx_state)
    1:if(local_tfr) tx_state<=2;
    2:tx_state<=3;
    3:if(local_tpr) begin if(tx_index==response_length-1) begin tx_state<=0;tx_index<=0;end else tx_index<=tx_index+1'b1;end
   endcase
   case(state)
    IDLE:if(fv && tx_state==0) begin
     length<=fl;header<=0;write_index<=0;state<=fl==0 ? DISPATCH : RECEIVE;
    end
    RECEIVE:if(pv) begin
     if(write_index<8) header[write_index*8 +:8]<=pd;
     if(pl) state<=DISPATCH;else write_index<=write_index+1'b1;
    end
    DISPATCH:begin
     queued_while_busy<=0;last_request_valid<=1;last_request_id<=header[31:16];
     s_diagnostic<=diagnostic_snapshot;bridge_snapshot<=bridge_diagnostic | {berrors,187'b0,motion_pending,36'b0};response_target<=header[15:8];
     s_model_crc32<=model_crc32;s_valid<=model_valid;s_loading<=loading;s_source<=source_pc;
     s_active_classes<=active_classes;s_generation<=generation;s_accepted<=accepted;s_wave_drops<=wave_drops;
     s_results<=results;s_wireless_bad<=wireless_bad;s_command_errors<=command_errors;s_crc_errors<=crc_errors;
     request_id<=header[31:16];opcode<=header[47:32];data_length<=header[63:48];
     status<=0;json_length<=0;response_length<=10;cursor<=8;scalar_index<=0;scalar<=0;field_index<=0;
     if(ft!=8'h50 || header[7:0]!=1 || length<8 || header[63:48]!=length-8) begin status<=3;state<=RESPOND;end
     else if(header[15:8]!=1 && header[15:8]!=2) begin status<=2;state<=RESPOND;end
     else if(header[15:8]==2) begin
      if(length>64 || (forward_stop && length!=8)) begin status<=3;state<=RESPOND;end
      else if(!forward_ready || (motion_request && motion_pending)) begin status<=4;state<=RESPOND;end
      else begin
       forward_start<=1;cursor<=0;state<=40;
       if(forward_stop) motion_pending<=0;
       else if(motion_request) begin motion_pending<=1;motion_id<=header[31:16];motion_opcode<=header[47:32];end
      end
     end
     else if(queued_while_busy && last_request_valid && header[31:16]==last_request_id) begin status<=4;state<=RESPOND;end
     else case(header[47:32])
      1:begin
       if(length!=8) status<=3;
       else begin
        json_kind<=1;json_length<=114;
       end
       state<=RESPOND;
      end
      2:begin
       if(length==8) begin json_kind<=2;json_length<=317;state<=RESPOND;end
       else if(length==9) begin after_read<=29;state<=ADDR;end
       else begin status<=3;state<=RESPOND;end
      end
      16'h100:if(length!=9) begin status<=3;state<=RESPOND;end else begin after_read<=20;state<=ADDR;end
      16'h101:if(length!=10) begin status<=3;state<=RESPOND;end else begin after_read<=21;state<=ADDR;end
      16'h102:if(length!=12+META_LEN || loading || !pipeline_idle) begin status<=loading || !pipeline_idle ? 4 : 3;state<=RESPOND;end
       else begin after_read<=22;state<=ADDR;end
      16'h103:if(!loading) begin status<=5;state<=RESPOND;end else if(length<19) begin status<=3;state<=RESPOND;end
       else begin after_read<=23;state<=ADDR;end
      16'h104:if(!loading || length!=12) begin status<=3;state<=RESPOND;end else begin after_read<=24;state<=ADDR;end
      16'h105:begin
       if(length!=8) status<=3;
       else if(loading) begin loading<=0;model_valid<=0;clear_pipeline<=1;end
       state<=RESPOND;
      end
      16'h108:if(!source_pc || !model_valid || loading) begin status<=5;state<=RESPOND;end
       else if(length<42) begin status<=3;state<=RESPOND;end
       else begin after_read<=25;state<=ADDR;end
      default:begin status<=6;state<=RESPOND;end
     endcase
    end
    ADDR:begin ca<=cursor;state<=READ;end
    READ:state<=USE;
    USE:begin
     cursor<=cursor+1'b1;
     case(after_read)
      20:begin
       if(cq>1) status<=3;
       else if(!pipeline_idle || loading) status<=4;
       else begin source_pc<=cq[0];clear_pipeline<=1;end
       state<=RESPOND;
      end
      21:begin
       if(scalar_index==0) begin scalar[7:0]<=cq;scalar_index<=1;state<=ADDR;end
       else begin if({cq,scalar[7:0]}==0) status<=3;else telemetry_div<={cq,scalar[7:0]};state<=RESPOND;end
      end
      22:begin
       scalar[scalar_index*8 +:8]<=cq;
       if(scalar_index==3) begin
        if({cq,scalar[23:0]}!=META_LEN) begin status<=3;state<=RESPOND;end
        else begin meta_index<=0;meta_bad<=0;seen_digit<=0;parsed_count<=0;crc32<=32'hffffffff;after_read<=26;state<=ADDR;end
       end else begin scalar_index<=scalar_index+1'b1;state<=ADDR;end
      end
      26:begin
       crc32<=crc_next;
       if(meta_index>=36 && meta_index<=40) begin
        if(cq>=48 && cq<=57) begin parsed_count<=(parsed_count<<3)+(parsed_count<<1)+cq-48;seen_digit<=1;end
        else if(cq!=32 || seen_digit) meta_bad<=1;
       end else if(cq!=meta_byte(meta_index)) meta_bad<=1;
       if(meta_index==META_LEN-1) begin
        if(meta_bad || cq!=meta_byte(meta_index) || !seen_digit || parsed_count<2 || parsed_count>CAPACITY) status<=8;
        else begin new_classes<=parsed_count;loading<=1;model_valid<=0;clear_pipeline<=1;next_bank<=0;next_address<=0;end
        state<=RESPOND;
       end else begin meta_index<=meta_index+1'b1;state<=ADDR;end
      end
      23:begin
       case(field_index)
        0:begin write_bank<=cq;field_index<=1;end
        1,2,3,4:begin write_address[(field_index-1)*8 +:8]<=cq;field_index<=field_index+1'b1;end
        5:begin word_count[7:0]<=cq;field_index<=6;end
        6:begin
         word_count[15:8]<=cq;word_left<={cq,word_count[7:0]};scalar_index<=0;after_read<=27;
         if(write_bank!={5'b0,next_bank} || write_address!=next_address || {cq,word_count[7:0]}==0 ||
           length!=15+({cq,word_count[7:0]}<<2) || write_address+{cq,word_count[7:0]}>bank_size(next_bank)) begin status<=3;state<=RESPOND;end
        end
       endcase
       if(!(field_index==6 && (write_bank!={5'b0,next_bank} || write_address!=next_address || {cq,word_count[7:0]}==0 ||
        length!=15+({cq,word_count[7:0]}<<2) || write_address+{cq,word_count[7:0]}>bank_size(next_bank)))) state<=ADDR;
      end
      27:begin
       word_value[scalar_index*8 +:8]<=cq;
       if(scalar_index==3) state<=WRITE_WORD;
       else begin scalar_index<=scalar_index+1'b1;state<=ADDR;end
      end
      24:begin
       scalar[scalar_index*8 +:8]<=cq;
       if(scalar_index==3) begin
        if(next_bank!=7 || next_address!=bank_size(7) || {cq,scalar[23:0]}!=(crc32^32'hffffffff)) status<=7;
        else begin model_crc32<=crc32^32'hffffffff;loading<=0;model_valid<=1;active_classes<=new_classes;generation<=generation+1'b1;clear_pipeline<=1;end
        state<=RESPOND;
       end else begin scalar_index<=scalar_index+1'b1;state<=ADDR;end
      end
      25:begin
       if(scalar_index==0) begin scalar[7:0]<=cq;scalar_index<=1;state<=ADDR;end
       else if({cq,scalar[7:0]}<1 || {cq,scalar[7:0]}>31 || length!=10+({cq,scalar[7:0]}<<5)) begin status<=3;state<=RESPOND;end
       else begin replay_left<={cq,scalar[7:0]};replay_count<={cq,scalar[7:0]};scalar_index<=0;replay_channel<=0;after_read<=28;state<=ADDR;end
      end
      29:begin
       if(cq>2) status<=3;
       else begin json_kind<=cq==0 ? 2 : (cq==1 ? 3 : 4);json_length<=cq==0 ? 317 : (cq==1 ? 173 : 84);end
       state<=RESPOND;
      end
      28:begin
       scalar[scalar_index*8 +:8]<=cq;
       if(scalar_index==3) begin
        if({cq[6:0],scalar[23]}==8'hff) begin status<=3;state<=RESPOND;end
        else begin
         replay_hold[(replay_channel<4 ? 3-replay_channel : replay_channel)*16 +:16]<=mv({cq,scalar[23:0]});
         scalar_index<=0;
         if(replay_channel==7) state<=REPLAY_NEXT;else begin replay_channel<=replay_channel+1'b1;state<=ADDR;end
        end
       end else begin scalar_index<=scalar_index+1'b1;state<=ADDR;end
      end
     endcase
    end
    WRITE_WORD:begin
     // Configuration fields that alter shifts/scales are explicitly bounded.
     if((next_bank==4 && ((next_address<144 && next_address[0]) || (next_address>=144 && next_address<152)) && word_value>31) ||
       (next_bank==4 && next_address==152 && word_value!=15) ||
       (next_bank==6 && next_address[0] && word_value>31)) begin status<=8;state<=RESPOND;end
     else begin
      parameter_write<=1;parameter_bank<=next_bank;parameter_address<=next_address;parameter_data<=word_value;
      crc_shift<={word_value,canonical_address,5'b0,next_bank};crc_left<=9;state<=CRC_BYTES;
      if(next_address==bank_size(next_bank)-1 && next_bank!=7) begin next_bank<=next_bank+1'b1;next_address<=0;end
      else next_address<=next_address+1'b1;
      scalar_index<=0;
      if(word_left==1) after_crc<=RESPOND;else begin word_left<=word_left-1'b1;after_crc<=ADDR;end
     end
    end
    CRC_BYTES:begin
     crc32<=crc_next;crc_shift<={8'b0,crc_shift[71:8]};crc_left<=crc_left-1'b1;
     if(crc_left==1) state<=after_crc;
    end
    REPLAY_NEXT:begin replay_data<=replay_hold;replay_valid<=1;state<=WAIT_REPLAY;end
    WAIT_REPLAY:if(replay_ready) begin
     replay_valid<=0;replay_channel<=0;
     if(replay_left==1) state<=RESPOND;else begin replay_left<=replay_left-1'b1;state<=ADDR;end
    end
    40:begin ca<=cursor;state<=41;end
    41:state<=42;
    42:begin
     // Copy only when the reserved queue is still accepting this packet.
     if(!forward_take) begin status<=4;state<=RESPOND;end
     else begin forward_valid<=1;state<=43;end
    end
    43:begin
     if(cursor==length-1) state<=IDLE;
     else begin cursor<=cursor+1'b1;state<=40;end
    end
    RESPOND:begin
     response_length<=10+json_length;
     if(status!=0) command_errors<=command_errors+1'b1;
     json_index<=0;state<=30;
    end
    30:begin tx_index<=0;tx_state<=1;state<=31;end
    31:if(tx_state==0 && bits_left==0 && !phy_valid) state<=IDLE;
   endcase
  end
 end
endmodule
`default_nettype wire
