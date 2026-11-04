`timescale 1ns/1ps
`default_nettype none
// Independent r50 candidate: A parallel commands/data, GPIO responses, four
// wave pages, nine Flash-backed poses, typed tasks and one shared SCS engine.
module b_tasks_top #(
 parameter integer BOOT_CLKS=262144,SW_CLKS=480000,FLASH_RELEASE_CLKS=2400000,SERVO_TIMEOUT_CLKS=480000,SERVO_GAP_CLKS=2400
)(
 input wire clk_24m,rst_n,host_uart_rx,display_sw,output wire host_uart_tx,b_return_tx,
 input wire [7:0] link_data,input wire link_clk,link_valid,
 output wire link_ready,input wire scs_rx,output wire scs_tx,
 input wire flash_miso,output wire flash_cs_n,flash_clk,flash_mosi,
 output wire vga_clk,vga_hs,vga_vs,output wire [7:0] vga_r,vga_g,vga_b,
 output wire [3:0] status_led_n
);
 reg [31:0] boot;wire boot_done=boot>=BOOT_CLKS-1;
 always @(posedge clk_24m or negedge rst_n)
  if(!rst_n) boot<=0; else if(!boot_done) boot<=boot+1'b1;
 wire ctrl_reset,pix_reset,clk_pix,clk_dac,locked;
 pcs_reset ctrl_sync(.clk(clk_24m),.async_rst_n(rst_n&&boot_done),.sync_rst_n(ctrl_reset));
 vga_pll_25m2 u_pll(.refclk(clk_24m),.reset(!rst_n),.clk_pix(clk_pix),.clk_dac(clk_dac),.locked(locked));
 pcs_reset pix_sync(.clk(clk_pix),.async_rst_n(ctrl_reset&&locked),.sync_rst_n(pix_reset));
 assign vga_clk=clk_dac;

 wire afv,afr,apv,apr,apl;wire [7:0] aft,apd;wire [10:0] afl;
 wire [31:0] a_frames,a_crc,a_gaps,a_timeouts,a_overflows,a_length,a_version,rx_bytes;
 wire heartbeat_timeout;
 link_rx_wrapper receiver(.clk_24m(clk_24m),.rst_n(ctrl_reset),.link_data(link_data),.link_clk(link_clk),
  .link_valid(link_valid),.link_ready(link_ready),.frame_valid(afv),.frame_ready(afr),
  .frame_type(aft),.frame_length(afl),.payload_valid(apv),.payload_ready(apr),.payload_data(apd),
  .payload_last(apl),.good_frame_count(a_frames),.rx_byte_count(rx_bytes),.crc_error_count(a_crc),
  .sequence_gap_count(a_gaps),.timeout_count(a_timeouts),.fifo_overflow_count(a_overflows),
  .length_error_count(a_length),.version_error_count(a_version),.heartbeat_timeout(heartbeat_timeout));
 wire display_enabled;
 b_tasks_switch #(.STABLE_CLKS(SW_CLKS)) sw(.clk(clk_24m),.rst_n(ctrl_reset),.display_sw(display_sw),.initialized(ctrl_reset),.enabled(display_enabled));
 wire [31:0] fault_stamp=a_crc+a_gaps+a_timeouts+a_overflows+a_length+a_version;
 reg [31:0] old_fault_stamp;
 always @(posedge clk_24m or negedge ctrl_reset)if(!ctrl_reset)old_fault_stamp<=0;else old_fault_stamp<=fault_stamp;
 wire transport_break=fault_stamp!=old_fault_stamp;
 wire decode_fr,decode_pr;
 wire sample_valid,result_valid,segment_break,result_fault,stale;wire [127:0] raw_data;
 wire [191:0] filtered_data,fast_data,slow_data;wire [31:0] sample_seq,window_seq,cnn_cycles,head_cycles,accepted,rejected,breaks;
 wire [15:0] sample_rate,generation,epoch,class_id,class_count,action_hint,margin;
 wire [7:0] wave_flags,result_flags;wire [31:0] reject_info;wire [255:0] rejected_payload;
 b_v2_decode a_decode(.clk(clk_24m),.rst_n(ctrl_reset),.frame_valid(afv && aft!=8'h42),.frame_ready(decode_fr),
  .frame_type(aft),.frame_length(afl),.payload_valid(apv && !parallel_command),.payload_ready(decode_pr),.payload_data(apd),.payload_last(apl),
  .wave_valid(sample_valid),.result_valid(result_valid),.segment_break(segment_break),.raw_mv(raw_data),
  .filtered_q8(filtered_data),.fast_q4(fast_data),.slow_q4(slow_data),.sample_seq(sample_seq),.window_seq(window_seq),
  .cnn_cycles(cnn_cycles),.head_cycles(head_cycles),.sample_rate(sample_rate),.generation(generation),.epoch(epoch),
  .class_id(class_id),.class_count(class_count),.action_hint(action_hint),.margin(margin),.wave_flags(wave_flags),
  .result_flags(result_flags),.stale(stale),.accepted(accepted),.rejected(rejected),.breaks(breaks),
  .result_fault(result_fault),.reject_info(reject_info),.rejected_payload(rejected_payload));

 // Daily commands arrive over A's validated parallel 0x42 link.
 // USB UART TX remains a passive mirror; omit the optional local RX parser.
 wire fv=1'b0,pv=1'b0,pl=1'b0;wire fr,pr;wire [7:0] ft=8'd0,pd=8'd0;wire [10:0] fl=11'd0;
 // Whole-frame command arbitration; waveform decoder never sees 0x42.
 reg cmd_busy,cmd_parallel,parallel_command;
 wire cmd_fr,cmd_pr;
 wire select_parallel=!cmd_busy && afv && aft==8'h42;
 wire cmd_fv=!cmd_busy && (select_parallel || fv);
 wire [7:0] cmd_ft=select_parallel?8'h42:ft;
 wire [10:0] cmd_fl=select_parallel?afl:fl;
 wire cmd_pv=cmd_busy && (cmd_parallel?apv:pv);
 wire [7:0] cmd_pd=cmd_parallel?apd:pd;
 wire cmd_pl=cmd_parallel?apl:pl;
 assign afr=aft==8'h42 ? (!cmd_busy && cmd_fr):decode_fr;
 assign apr=parallel_command?(cmd_busy && cmd_parallel && cmd_pr):decode_pr;
 assign fr=!cmd_busy && !select_parallel && cmd_fr;
 assign pr=cmd_busy && !cmd_parallel && cmd_pr;
 always @(posedge clk_24m or negedge ctrl_reset)begin
  if(!ctrl_reset)begin cmd_busy<=0;cmd_parallel<=0;parallel_command<=0;end
  else begin
   if(afv && afr)parallel_command<=aft==8'h42;
   if(cmd_fv && cmd_fr)begin cmd_busy<=cmd_fl!=0;cmd_parallel<=select_parallel;end
   if(cmd_pv && cmd_pr && cmd_pl)cmd_busy<=0;
  end
 end
 assign b_return_tx=host_uart_tx;
 wire command_valid,command_ready,response_valid,response_ready;wire [15:0] request_id,opcode,data_length;
 wire [79:0] arguments;wire [7:0] response_type;wire [15:0] response_id,response_opcode,response_status;
 wire [9:0] response_length;wire [4095:0] response_data;wire [31:0] pc_rejects;
 b_v2_pc pc(.clk(clk_24m),.rst_n(ctrl_reset),.frame_valid(cmd_fv),.frame_ready(cmd_fr),.frame_type(cmd_ft),.frame_length(cmd_fl),
  .payload_valid(cmd_pv),.payload_ready(cmd_pr),.payload_data(cmd_pd),.payload_last(cmd_pl),.command_valid(command_valid),
  .command_ready(command_ready),.request_id(request_id),.opcode(opcode),.data_length(data_length),.arguments(arguments),
  .response_valid(response_valid),.response_ready(response_ready),.response_type(response_type),.response_id(response_id),
  .response_opcode(response_opcode),.response_status(response_status),.response_length(response_length),
  .response_data(response_data),.response_byte(response_byte),.response_byte_index(response_byte_index),.uart_tx(host_uart_tx),.rejects(pc_rejects));
 wire [9:0] response_byte_index;wire [7:0] response_byte;
 wire [2:0] page;wire freeze,armed,teach_mode;wire [127:0] gains,ranges;wire [7:0] custom;wire [15:0] revision;
 wire flash_init_done,flash_available,flash_load_valid,flash_save_req,flash_save_done,flash_save_ok;
 wire [3:0] flash_load_group,flash_save_group;wire [49:0] flash_load_positions,flash_save_positions;
 reg [31:0] flash_boot;
 always @(posedge clk_24m or negedge ctrl_reset)if(!ctrl_reset)flash_boot<=0;else if(flash_boot<FLASH_RELEASE_CLKS)flash_boot<=flash_boot+1'b1;
 wire flash_miso_captured;
 b_tasks_flash_input flash_capture(.clk(clk_24m),.miso(flash_miso),.captured(flash_miso_captured));
 teach_flash_store flash_store(.clk(clk_24m),.rst_n(ctrl_reset && flash_boot>=FLASH_RELEASE_CLKS),
  .save_req(flash_save_req),.save_group(flash_save_group),.save_positions(flash_save_positions),
  .init_done(flash_init_done),.available(flash_available),.load_valid(flash_load_valid),.load_group(flash_load_group),
  .load_positions(flash_load_positions),.save_done(flash_save_done),.save_ok(flash_save_ok),
  .flash_cs_n(flash_cs_n),.flash_clk(flash_clk),.flash_mosi(flash_mosi),.flash_miso(flash_miso_captured));
 // uart_rx_byte inside the shared engine owns the two-flop RX synchronizer.
 wire scs_sync=scs_rx;
 wire req_valid,req_ready,event_valid,event_ready,servo_idle;wire [2:0] req_op;
 wire [15:0] req_id,req_arg,event_id,event_status;wire [3:0] query_pose;wire query_valid;wire [49:0] query_positions;
 wire [4:0] online,torque,torque_valid_mask,active_mask,retired_mask,feedback_valid_mask;wire [79:0] positions,speeds,loads,feedback_age_ms;wire [8:0] pose_valid;
 wire [7:0] motion_state;wire [31:0] fault_flags,auto_count,auto_rejects;wire servo_data,servo_enable;
 b_tasks_servo #(.TIMEOUT_CLKS(SERVO_TIMEOUT_CLKS),.GAP_CLKS(SERVO_GAP_CLKS)) servo_controller(
  .clk(clk_24m),.rst_n(ctrl_reset),.servo_rx(scs_sync),.servo_tx(servo_data),.servo_tx_enable(servo_enable),
  .req_valid(req_valid),.req_ready(req_ready),.req_op(req_op),.req_id(req_id),.req_arg(req_arg),
  .event_valid(event_valid),.event_ready(event_ready),.event_id(event_id),.event_status(event_status),.idle(servo_idle),
  .query_pose(query_pose),.query_valid(query_valid),.query_positions(query_positions),
  .flash_init_done(flash_init_done),.flash_available(flash_available),.flash_load_valid(flash_load_valid),
  .flash_load_group(flash_load_group),.flash_load_positions(flash_load_positions),.flash_save_req(flash_save_req),
  .flash_save_group(flash_save_group),.flash_save_positions(flash_save_positions),.flash_save_done(flash_save_done),.flash_save_ok(flash_save_ok),
  .online(online),.torque(torque),.torque_valid_mask(torque_valid_mask),.positions(positions),.speeds(speeds),.loads(loads),.feedback_age_ms(feedback_age_ms),.feedback_valid_mask(feedback_valid_mask),.pose_valid(pose_valid),
  .motion_state(motion_state),.active_mask(active_mask),.retired_mask(retired_mask),.fault_flags(fault_flags));
 assign scs_tx=servo_enable?servo_data:1'b1;
 b_tasks_control control(.clk(clk_24m),.rst_n(ctrl_reset),.display_enabled(display_enabled),
  .command_valid(command_valid),.command_ready(command_ready),.request_id(request_id),.opcode(opcode),.data_length(data_length),.arguments(arguments),
  .response_valid(response_valid),.response_ready(response_ready),.response_type(response_type),.response_id(response_id),.response_opcode(response_opcode),
  .response_status(response_status),.response_length(response_length),.response_data(response_data),.response_byte(response_byte),.response_byte_index(response_byte_index),
  .result_valid(result_valid),.wave_valid(sample_valid),.link_fault(transport_break || result_fault || heartbeat_timeout),.stale(stale),
  .generation(generation),.epoch(epoch),.class_id(class_id),.class_count(class_count),.result_flags(result_flags),.window_seq(window_seq),
  .online(online),.torque(torque),.pose_valid(pose_valid),.servo_idle(servo_idle),.flash_ready(flash_available && flash_init_done),
  .flash_available(flash_available),.flash_initialized(flash_init_done),.positions(positions),.speeds(speeds),.loads(loads),.feedback_age_ms(feedback_age_ms),.feedback_valid_mask(feedback_valid_mask),.torque_valid_mask(torque_valid_mask),
  .motion_state(motion_state),.active_mask(active_mask),.retired_mask(retired_mask),.fault_flags(fault_flags),
  .req_valid(req_valid),.req_ready(req_ready),.req_op(req_op),.req_id(req_id),.req_arg(req_arg),
  .event_valid(event_valid),.event_ready(event_ready),.event_id(event_id),.event_status(event_status),
  .query_pose(query_pose),.query_valid(query_valid),.query_positions(query_positions),
  .page(page),.freeze(freeze),.armed(armed),.teach_mode(teach_mode),.ranges(ranges),.gains(gains),.custom(custom),.revision(revision),
  .auto_count(auto_count),.auto_rejects(auto_rejects));

 // Classification validity is independent of the waveform sample sequence.
 // Only waveform discontinuities invalidate the waveform history.
 wire history_break=segment_break || transport_break;wire fifo_full,fifo_empty,scaled_valid;wire [160:0] scaled_data,fifo_data;
 wire [31:0] display_drops;wire saturated;wire [15:0] busy_drops,full_drops;wire [4:0] display_fifo_level;
 b_tasks_scale scale(.clk(clk_24m),.rst_n(ctrl_reset),.sample_valid(sample_valid),.segment_break(history_break),.raw_mv(raw_data),
  .filtered_q8(filtered_data),.fast_q4(fast_data),.slow_q4(slow_data),.page(page),.gains(gains),.ranges(ranges),.custom(custom),.revision(revision),
  .fifo_full(fifo_full),.valid(scaled_valid),.data(scaled_data),.dropped(display_drops),.saturated(saturated),
  .busy_drops(busy_drops),.full_drops(full_drops));
 wire fifo_ctrl_reset;pcs_reset fifo_sync(.clk(clk_24m),.async_rst_n(ctrl_reset&&locked),.sync_rst_n(fifo_ctrl_reset));
 link_async_fifo #(.DATA_WIDTH(161),.ADDR_WIDTH(4)) sample_fifo(.wr_clk(clk_24m),.wr_rst_n(fifo_ctrl_reset),
  .wr_en(scaled_valid&&!fifo_full),.wr_data(scaled_data),.wr_full(fifo_full),.wr_level(display_fifo_level),
  .rd_clk(clk_pix),.rd_rst_n(pix_reset),.rd_en(!fifo_empty),.rd_data(fifo_data),.rd_empty(fifo_empty));

 reg [679:0] display_meta;wire [679:0] pix_meta;wire frame_start;wire meta_ready;
 wire [319:0] servo_snapshot={26'd0,torque_valid_mask,armed,motion_state,pose_valid,feedback_valid_mask,torque,online,loads,speeds,positions,16'd0};
 always @* display_meta={servo_snapshot,custom,ranges,42'd0,gains,saturated,stale,freeze,page,generation,sample_rate,revision};
 b_v2_mailbox #(.WIDTH(680)) meta_mailbox(.src_clk(clk_24m),.src_rst_n(fifo_ctrl_reset),.src_valid(1'b1),
  .src_ready(meta_ready),.src_data(display_meta),.dst_clk(clk_pix),.dst_rst_n(pix_reset),.dst_commit(frame_start),
  .dst_data(pix_meta),.dst_pulse());
 reg [144:0] fifo_capture;reg capture_valid;
 always @(posedge clk_pix)fifo_capture<=fifo_data[144:0];
 always @(posedge clk_pix or negedge pix_reset)if(!pix_reset)capture_valid<=0;else capture_valid<=!fifo_empty;
 reg [3:0] revision_match;reg capture_valid2,capture_clear2;reg [127:0] capture_data2;
 genvar meta_part;
 generate for(meta_part=0;meta_part<4;meta_part=meta_part+1)begin:compare_revision
  always @(posedge clk_pix)revision_match[meta_part]<=fifo_capture[129+meta_part*4+:4]==pix_meta[meta_part*4+:4];
 end endgenerate
 always @(posedge clk_pix)begin capture_data2<=fifo_capture[127:0];capture_clear2<=fifo_capture[128];end
 always @(posedge clk_pix or negedge pix_reset)if(!pix_reset)capture_valid2<=0;else capture_valid2<=capture_valid;
 reg [127:0] pix_data;reg pix_valid,pix_clear;reg [15:0] last_revision;
 always @(posedge clk_pix)pix_data<=capture_data2;
 always @(posedge clk_pix or negedge pix_reset)begin
  if(!pix_reset)begin pix_valid<=0;pix_clear<=0;last_revision<=0;end
  else begin
   pix_valid<=0;pix_clear<=0;last_revision<=pix_meta[15:0];
   if(last_revision!=pix_meta[15:0])pix_clear<=1;
   else if(capture_valid2 && &revision_match && !pix_meta[51])begin
    pix_clear<=capture_clear2;pix_valid<=!capture_clear2;
   end
  end
 end
 wire [15:0] pix_revision=pix_meta[15:0];
 wire [15:0] pix_rate=pix_meta[31:16],pix_generation=pix_meta[47:32];
 wire [2:0] pix_page=pix_meta[50:48];wire pix_freeze=pix_meta[51];
 wire pix_stale=pix_meta[52],pix_saturated=pix_meta[53];wire [127:0] pix_gains=pix_meta[181:54];
 b_v2_renderer renderer(.clk(clk_pix),.rst_n(pix_reset),.sample_valid(pix_valid),.sample_data(pix_data),
  .history_clear(pix_clear),.freeze(pix_freeze),.page(pix_page),.channel_gains(pix_gains),.ranges(pix_meta[351:224]),.custom(pix_meta[359:352]),.gain(pix_gains[15:0]),
  .sample_rate(pix_rate),.class_id(16'hffff),.generation(pix_generation),.mapped_action(16'hffff),.mapped_pose(16'hffff),
  .armed(pix_meta[648]),.saturated(pix_saturated),.link_stale(pix_stale),.servo_snapshot(pix_meta[679:360]),.vga_r(vga_r),.vga_g(vga_g),
  .vga_b(vga_b),.vga_hs(vga_hs),.vga_vs(vga_vs),.vga_clk(),.frame_start(frame_start),.sample_count(),.frame_count(),.wave_valid());
 assign status_led_n=~{(|fault_flags),locked,(|online),display_enabled};
endmodule
module pcs_reset(input wire clk,async_rst_n,output wire sync_rst_n);
 (* preserve="true",async_reg="true" *) reg [1:0] sync_pipe;
 always @(posedge clk or negedge async_rst_n) if(!async_rst_n)sync_pipe<=0;else sync_pipe<={sync_pipe[0],1'b1};
 assign sync_rst_n=sync_pipe[1];
endmodule
`default_nettype wire
