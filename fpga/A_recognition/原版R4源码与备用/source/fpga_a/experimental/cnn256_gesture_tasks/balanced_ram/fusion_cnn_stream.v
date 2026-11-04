`timescale 1ns/1ps
`default_nettype none
// Statistics and learned CNN256 scores jointly drive the final decision.
module fusion_cnn_stream #(
    parameter integer CLASSES=7,parameter integer EXPORT_SCORES=0
) (
    input wire clk,input wire rst_n,input wire [15:0] active_classes,input wire [31:0] sample_seq,
    input wire sample_valid,output wire sample_ready,input wire [127:0] sample_mv,
    output wire result_valid,input wire result_ready,
    output wire [15:0] raw_class,output wire [15:0] stable_class,output wire [15:0] cnn_class,
    output wire [15:0] action_id,output wire [8:0] margin_q8,
    output wire [CLASSES*48-1:0] baseline_scores,output wire [CLASSES*48-1:0] cnn_scores,
    output wire [31:0] accepted_samples,output wire [31:0] window_end,
    output wire [31:0] saturations,output wire [31:0] cnn_cycles,output wire [31:0] cnn_mac_cycles,
    output wire [31:0] head_cycles,output wire [31:0] feature_cycles,
    input wire parameter_write,input wire [2:0] parameter_bank,
    input wire [31:0] parameter_address,input wire [31:0] parameter_data,
    output wire parameter_ready,
    output wire telemetry_valid,output reg [31:0] telemetry_sample_seq,output reg [31:0] result_sample_seq,output wire [127:0] telemetry_raw,
    output wire [191:0] telemetry_filtered,telemetry_fast,telemetry_slow
);
    wire [191:0] q8;
    genvar g;
    generate for(g=0;g<8;g=g+1) begin:input_adapter
        wire [15:0] raw=sample_mv[g*16 +:16];
        wire signed [16:0] centered=$signed({1'b0,raw>3300 ? 16'd3300 : raw})-17'sd1650;
        assign q8[g*24 +:24]=centered<<<8;
    end endgenerate
    reg [127:0] raw_filter,raw_features;reg [31:0] seq_filter;
    always @(posedge clk) begin
        if(sample_valid && sample_ready) begin raw_filter<=sample_mv;seq_filter<=sample_seq;end
        if(fv && fr) begin raw_features<=raw_filter;telemetry_sample_seq<=seq_filter;end
        if(start) result_sample_seq<=telemetry_sample_seq;
    end
    assign telemetry_raw=raw_features;
    wire fv,fr;wire [767:0] filtered;
    hybrid_filterbank #(.FRONT(2)) filter_unit(clk,rst_n,sample_valid,sample_ready,q8,fv,fr,filtered,saturations);
    wire fw,ww,wv,wr;wire [8:0] fa;wire signed [15:0] fd;
    wire [10:0] wa;wire signed [20:0] wd;
    wire cr,hr,cv,ew;wire [4:0] ea;wire signed [7:0] ed;
    wire start=wv && wr;
    assign wr=cr && hr;
    assign parameter_ready=cr && hr && fr && sample_ready && !fv && !sample_valid && !wv;
    fusion_features features_unit(clk,rst_n,fv,fr,filtered,fw,fa,fd,ww,wa,wd,wv,wr,
        accepted_samples,window_end,feature_cycles,telemetry_valid,telemetry_filtered,telemetry_fast,telemetry_slow);
    cnn32_core cnn_unit(clk,rst_n,ww,wa,wd,start,cr,ew,ea,ed,cv,1'b1,cnn_cycles,cnn_mac_cycles,
        parameter_write,parameter_bank,parameter_address,parameter_data,/*debug_valid*/,
        /*debug_layer*/,/*debug_address*/,/*debug_data*/);
    fusion_head #(.CLASSES(CLASSES),.EXPORT_SCORES(EXPORT_SCORES)) head_unit(clk,rst_n,active_classes,fw,fa,fd,ew,ea,ed,start,hr,cv,
        result_valid,result_ready,raw_class,stable_class,cnn_class,action_id,margin_q8,
        baseline_scores,cnn_scores,head_cycles,parameter_write,parameter_bank,parameter_address,parameter_data);
endmodule
`default_nettype wire
