`default_nettype none
module v2_board_top #(parameter integer CAPACITY=7,parameter integer UART_BIT_CYCLES=26)(
 input wire clk_24m,input wire rst_n,input wire pc_uart_rx,output wire pc_uart_tx,
 input wire wireless_uart_rx,input wire b_return_rx,
 output wire [7:0] link_data,output wire link_clk,output wire link_valid,input wire link_ready,
 output wire [3:0] status_led_n
);
 reg [7:0] startup=0;
 always @(posedge clk_24m or negedge rst_n) begin
  if(!rst_n) startup<=0;else startup<={startup[6:0],1'b1};
 end
 wire reset_n;
 reset_sync_n reset_unit(clk_24m,rst_n && startup[7],reset_n);
 reg [31:0] uptime;reg [4:0] us_div;
 always @(posedge clk_24m or negedge reset_n) begin
  if(!reset_n) begin uptime<=0;us_div<=0;end
  else if(us_div==23) begin us_div<=0;uptime<=uptime+1'b1;end else us_div<=us_div+1'b1;
 end
 wire source_pc,model_valid,loading,clear_pipeline,parameter_write,parameter_ready;
 wire [15:0] generation,active_classes,telemetry_div;
 wire [2:0] parameter_bank;wire [31:0] parameter_address,parameter_data;
 wire replay_valid,replay_ready;wire [127:0] replay_data;
 wire sv,sr,discontinuity;wire [127:0] raw;
 wire [31:0] good,bad,diagnostic,overflows,command_errors;
 wire [31:0] wave_drops,result_drops,accepted,window_end,saturations,cnn_cycles,mac_cycles,head_cycles,feature_cycles;
 wire [511:0] diagnostic_snapshot={telemetry_div,active_classes,generation,epoch,result_count,sample_sequence,
  feature_cycles,head_cycles,cnn_cycles,result_drops,wave_drops,saturations,diagnostic,overflows,bad,good,
  8'b0,3'b0,(raw_class!=cnn_class),discontinuity,source_pc,loading,model_valid,16'd6,32'h32564241};
 reg [31:0] result_count;
 wire stream_valid,stream_ready,stream_pv,stream_pr;wire [7:0] stream_type,stream_data;
 wire [10:0] stream_length;wire [15:0] stream_drops;wire [4:0] stream_queued;
 wire [223:0] pc_raw_payload={traw,epoch,generation,tseq,8'b0,8'd8,wave_flags,8'd1};
 gt_usb_stream usb_stream(clk_24m,reset_n,pipeline_clear,tv && core_reset,pc_raw_payload,
  (rv && core_reset) || pipeline_clear,pipeline_clear ? invalid_result : result_payload,
  stream_valid,stream_ready,stream_type,stream_length,stream_pv,stream_pr,stream_data,stream_drops,stream_queued);
 wire forward_start,forward_stop,forward_ready,forward_valid,forward_take;
 wire [6:0] forward_length;wire [7:0] forward_data;
 wire mfv,mfr,mstop,mpv,mpr;wire [10:0] mfl;wire [7:0] mpd;
 wire [15:0] cancelled;wire [3:0] queue_state;
 gt_forward forwarding(clk_24m,reset_n,pipeline_clear,forward_start,forward_stop,forward_length,
  forward_ready,forward_valid,forward_take,forward_data,mfv,mfr,mstop,mfl,mpv,mpr,mpd,cancelled,queue_state);
 wire [255:0] bridge_diagnostic={32'b0,32'b0,32'b0,32'b0,27'b0,stream_queued,16'b0,stream_drops,cancelled,12'b0,queue_state,32'h31544741};

 v2_pc #(.CAPACITY(CAPACITY),.BIT_CYCLES(UART_BIT_CYCLES)) pc(
  .clk(clk_24m),.rst_n(reset_n),.rx(pc_uart_rx),.tx(pc_uart_tx),.pipeline_idle(parameter_ready || !model_valid),.timestamp(uptime),
  .accepted(accepted),.wave_drops(wave_drops),.results(result_count),.wireless_bad(bad+overflows),.diagnostic_snapshot(diagnostic_snapshot),
  .source_pc(source_pc),.telemetry_div(telemetry_div),.model_valid(model_valid),.loading(loading),
  .clear_pipeline(clear_pipeline),.generation(generation),.active_classes(active_classes),
  .parameter_write(parameter_write),.parameter_bank(parameter_bank),.parameter_address(parameter_address),.parameter_data(parameter_data),
  .replay_valid(replay_valid),.replay_ready(replay_ready),.replay_data(replay_data),.command_errors(command_errors),.forward_start(forward_start),.forward_stop(forward_stop),.forward_length(forward_length),
  .forward_ready(forward_ready),.forward_valid(forward_valid),.forward_take(forward_take),.forward_data(forward_data),
  .b_rx(b_return_rx),.bridge_diagnostic(bridge_diagnostic),
  .stream_valid(stream_valid),.stream_ready(stream_ready),.stream_type(stream_type),.stream_length(stream_length),
  .stream_payload_valid(stream_pv),.stream_payload_ready(stream_pr),.stream_data(stream_data));
 justfloat_ingress #(.CLKS_PER_BIT(UART_BIT_CYCLES)) ingress(
  clk_24m,reset_n,!source_pc && !loading && model_valid,wireless_uart_rx,uptime,sv,sr,raw,
  /*seq*/,/*timestamp*/,discontinuity,good,bad,diagnostic,overflows);
 reg [31:0] bad_seen;
 always @(posedge clk_24m or negedge reset_n) begin
  if(!reset_n) bad_seen<=0;else bad_seen<=bad;
 end
 wire pipeline_clear=clear_pipeline || discontinuity || (!source_pc && bad!=bad_seen);
 wire core_reset=reset_n && model_valid && !pipeline_clear;
 reg [15:0] epoch;reg [31:0] sample_sequence;
 wire input_valid=(source_pc ? replay_valid : sv) && model_valid && !loading && !pipeline_clear;
 wire input_ready;
 wire [127:0] input_data=source_pc ? replay_data : raw;
 assign sr=input_ready && !source_pc && model_valid && !loading && !pipeline_clear;
 assign replay_ready=input_ready && source_pc && model_valid && !loading && !pipeline_clear;
 always @(posedge clk_24m or negedge reset_n) begin
  if(!reset_n) begin epoch<=0;sample_sequence<=0;end
  else begin
   if(pipeline_clear) epoch<=epoch+1'b1;
   if(input_valid && input_ready) sample_sequence<=sample_sequence+1'b1;
  end
 end
 wire rv,tv;wire [15:0] raw_class,stable_class,cnn_class,action;wire [8:0] margin;
 wire [CAPACITY*48-1:0] scores,cnn_scores;
 wire [127:0] traw;wire [191:0] tfilt,tfast,tslow;wire [31:0] tseq,rseq;
 fusion_cnn_stream #(.CLASSES(CAPACITY)) core(
  .clk(clk_24m),.rst_n(core_reset),.active_classes(active_classes),.sample_seq(sample_sequence),
  .sample_valid(input_valid),.sample_ready(input_ready),.sample_mv(input_data),
  .result_valid(rv),.result_ready(1'b1),.raw_class(raw_class),.stable_class(stable_class),.cnn_class(cnn_class),
  .action_id(action),.margin_q8(margin),.baseline_scores(scores),.cnn_scores(cnn_scores),
  .accepted_samples(accepted),.window_end(window_end),.saturations(saturations),.cnn_cycles(cnn_cycles),
  .cnn_mac_cycles(mac_cycles),.head_cycles(head_cycles),.feature_cycles(feature_cycles),
  .parameter_write(parameter_write),.parameter_bank(parameter_bank),.parameter_address(parameter_address),.parameter_data(parameter_data),
  .parameter_ready(parameter_ready),.telemetry_valid(tv),.telemetry_sample_seq(tseq),.result_sample_seq(rseq),
  .telemetry_raw(traw),.telemetry_filtered(tfilt),.telemetry_fast(tfast),.telemetry_slow(tslow));
 reg [31:0] epoch_samples;reg [15:0] div_count;
 always @(posedge clk_24m or negedge core_reset) begin
  if(!core_reset) begin epoch_samples<=0;div_count<=0;result_count<=0;end
  else begin
   if(rv) result_count<=result_count+1'b1;
   if(tv) begin
    epoch_samples<=epoch_samples+1'b1;
    if(div_count>=telemetry_div-1) div_count<=0;else div_count<=div_count+1'b1;
   end
  end
 end
 wire [7:0] wave_flags={5'b0,source_pc,epoch_samples<320,epoch_samples==0};
 wire [831:0] wave_payload={tslow,tfast,tfilt,traw,16'b0,epoch,generation,16'd1000,tseq,wave_flags,8'd8,8'h0f,8'd1};
 wire [255:0] result_payload={32'b0,head_cycles,cnn_cycles,16'b0,epoch,active_classes,generation,rseq,7'b0,margin,action,stable_class,6'b0,model_valid,model_valid,8'd1};
 wire [255:0] invalid_result={32'b0,32'b0,32'b0,16'b0,(epoch+16'd1),active_classes,generation,
  (sample_sequence-32'd1),16'b0,16'hffff,16'hffff,6'b0,model_valid,1'b0,8'd1};
 wire fv,fr,pv,pr;wire [7:0] ft,pd;wire [10:0] fl;wire [31:0] fts,frames,tx_errors;
 v2_packet_mux mux(clk_24m,reset_n,pipeline_clear,uptime,tv && div_count==0 && core_reset,wave_payload,
  (rv && core_reset) || pipeline_clear,pipeline_clear ? invalid_result : result_payload,wave_drops,result_drops,fv,fr,ft,fl,fts,pv,pr,pd,mfv,mfr,mstop,mfl,mpv,mpr,mpd);
 link_tx_wrapper tx(.clk_24m(clk_24m),.rst_n(reset_n),.frame_valid(fv),.frame_ready(fr),
  .frame_type(ft),.frame_timestamp(fts),.frame_length(fl),.payload_valid(pv),.payload_ready(pr),
  .payload_data(pd),.test_mode(2'b0),.link_data(link_data),.link_clk(link_clk),.link_valid(link_valid),
  .link_ready(link_ready),.tx_frame_count(frames),.error_count(tx_errors));
 assign status_led_n=~{loading,source_pc,result_count!=0,model_valid};
endmodule
`default_nettype wire
