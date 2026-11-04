`timescale 1ns/1ps
`default_nettype none
// Three genuine learned convolutions. A 32-lane shared INT8 array consumes
// one synchronous activation/weight word per clock in the dot-product phase.
// Temporal and input-channel reuse replaces three mostly-idle layer arrays.
module cnn32_core (
    input wire clk,input wire rst_n,
    input wire wave_write,input wire [10:0] wave_address,input wire signed [20:0] wave_data,
    input wire start,output wire ready,
    output reg embedding_write,output reg [4:0] embedding_address,output reg signed [7:0] embedding_data,
    output reg result_valid,input wire result_ready,
    output reg [31:0] cycles,output reg [31:0] mac_issue_cycles,
    input wire parameter_write,input wire [2:0] parameter_bank,
    input wire [31:0] parameter_address,input wire [31:0] parameter_data,
    output reg debug_valid,output reg [1:0] debug_layer,
    output reg [11:0] debug_address,output reg signed [7:0] debug_data
);
    localparam IDLE=0,BEGIN_DOT=1,PRIME=2,RUN=3,OUT_PRIME=4,OUT_RUN=5,GAP=6;
    reg [2:0] state;
    reg [1:0] layer;
    reg [7:0] time_index;
    reg [6:0] issue_term,consume_term;
    reg [4:0] issue_channel;
    reg [2:0] issue_kernel;
    reg [4:0] output_issue,output_lane,gap_channel;
    wire [8:0] width=layer==0 ? 9'd256 : layer==1 ? 9'd128 : 9'd64;
    wire [6:0] terms=layer==0 ? 7'd40 : layer==1 ? 7'd48 : 7'd72;
    wire [5:0] channels=layer==0 ? 6'd16 : layer==1 ? 6'd24 : 6'd32;
    wire [7:0] weight_base=layer==0 ? 8'd0 : layer==1 ? 8'd40 : 8'd88;
    wire [6:0] config_base=layer==0 ? 7'd0 : layer==1 ? 7'd16 : 7'd40;
    wire [7:0] weight_address=weight_base+issue_term;
    wire signed [9:0] input_time=$signed({2'b0,time_index})+$signed({7'b0,issue_kernel})-(layer==0 ? 10'sd2 : 10'sd1);
    wire padding=input_time<0 || input_time>=$signed({1'b0,width});
    wire [10:0] activation_address=(issue_channel<<(layer==0 ? 8 : layer==1 ? 7 : 6))+input_time;
    (* ram_style="bram_32k" *) reg signed [7:0] a0[0:2047];
    (* ram_style="bram_32k" *) reg signed [7:0] a1[0:2047];
    // a0 is reused for pool2 after the input is no longer needed.
    reg signed [7:0] activation_q0,activation_q1;
    reg padding_q;
    wire signed [7:0] activation=padding_q ? 8'sd0 : layer==1 ? activation_q1 : activation_q0;
    wire [255:0] weights;
    reg signed [31:0] biases[0:71];reg [7:0] shifts[0:71];reg [7:0] input_shifts[0:7];
    reg signed [31:0] bias_q;reg [7:0] shift_q;
    initial begin
        $readmemh("cnn_bias.hex",biases);$readmemh("cnn_shift.hex",shifts);
        $readmemh("cnn_input_shift.hex",input_shifts);
    end
    wire [6:0] config_address=config_base+output_issue;
    function signed [7:0] normalize;
        input signed [20:0] value;input [7:0] shift;
        reg [31:0] magnitude,rounded;
        begin
            magnitude=value<0 ? -$signed({value[20],value}) : $signed({1'b0,value});
            rounded=shift==0 ? magnitude : (magnitude+(32'd1<<(shift[4:0]-1'b1)))>>shift[4:0];
            if(rounded>127) normalize=value<0 ? -8'sd127 : 8'sd127;
            else normalize=value<0 ? -$signed(rounded[7:0]) : $signed(rounded[7:0]);
        end
    endfunction
    always @(posedge clk) begin
        if(wave_write && ready) a0[wave_address]<=normalize(wave_data,input_shifts[wave_address[10:8]]);
        else if(state==OUT_RUN && layer==1 && time_index[0])
            a0[{output_lane,time_index[6:1]}]<=
                quantized>pool_previous[output_lane] ? quantized : pool_previous[output_lane];
        activation_q0<=a0[activation_address];activation_q1<=a1[activation_address];
        padding_q<=padding;
        bias_q<=biases[config_address];shift_q<=shifts[config_address];
        if(parameter_write && parameter_bank==4) begin
            if(parameter_address<144) begin
                if(parameter_address[0]) shifts[parameter_address[7:1]]<=parameter_data[7:0];
                else biases[parameter_address[7:1]]<=parameter_data;
            end else if(parameter_address<152) input_shifts[parameter_address-144]<=parameter_data[7:0];
        end
    end
    genvar g;
    generate for(g=0;g<8;g=g+1) begin:weight_bank
        (* ram_style="bram" *) reg [31:0] memory[0:255];
        reg [31:0] q;
        if(g==0) initial $readmemh("cnn_w0.hex",memory);
        if(g==1) initial $readmemh("cnn_w1.hex",memory);
        if(g==2) initial $readmemh("cnn_w2.hex",memory);
        if(g==3) initial $readmemh("cnn_w3.hex",memory);
        if(g==4) initial $readmemh("cnn_w4.hex",memory);
        if(g==5) initial $readmemh("cnn_w5.hex",memory);
        if(g==6) initial $readmemh("cnn_w6.hex",memory);
        if(g==7) initial $readmemh("cnn_w7.hex",memory);
        always @(posedge clk) begin
            q<=memory[weight_address];
            if(parameter_write && parameter_bank==3 && parameter_address[2:0]==g)
                memory[parameter_address[10:3]]<=parameter_data;
        end
        assign weights[g*32 +:32]=q;
    end endgenerate
    wire signed [31:0] sums[0:31];
    generate for(g=0;g<32;g=g+1) begin:pe
        // At most72 products of signed INT8 values: abs(sum)<=72*127*127
        // =1,161,288. Signed22 is exact; bias is added later in signed33.
        reg signed [21:0] accumulator;
        wire signed [15:0] product=activation*$signed(weights[g*8 +:8]);
        // BEGIN_DOT initializes every accumulator before PRIME/RUN can read
        // it. A separate asynchronous datapath reset is unnecessary and makes
        // the board reset net difficult to route through the dense PE array.
        always @(posedge clk) begin
            if(state==BEGIN_DOT) accumulator<=0;
            else if(state==RUN) accumulator<=accumulator+{{6{product[15]}},product};
        end
        assign sums[g]={{10{accumulator[21]}},accumulator};
    end endgenerate
    reg signed [7:0] pool_previous[0:31];
    reg [15:0] gap_sum[0:31];
    reg signed [32:0] biased;
    reg [32:0] shifted;
    reg signed [7:0] quantized;
    always @* begin
        biased=$signed(sums[output_lane])+$signed(bias_q);
        shifted=shift_q==0 ? biased : ($signed(biased)+(33'd1<<(shift_q[4:0]-1'b1)))>>shift_q[4:0];
        if(biased<=0) quantized=0;
        else if(shifted>127) quantized=127;
        else quantized=shifted[7:0];
    end
    assign ready=state==IDLE && !result_valid;
    integer i;
    integer reset_gap;
    // Valid-state initialization: GAP is cleared on each accepted start;
    // pool_previous is written at every even time before the odd time uses it.
    // Only the control FSM needs the external asynchronous reset.
    always @(posedge clk) begin
        if(state==IDLE && start && ready)
            for(reset_gap=0;reset_gap<32;reset_gap=reset_gap+1) gap_sum[reset_gap]<=0;
        else if(state==OUT_RUN && layer==2)
            gap_sum[output_lane]<=gap_sum[output_lane]+quantized;
        if(state==OUT_RUN && layer!=2 && !time_index[0])
            pool_previous[output_lane]<=quantized;
    end
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=IDLE;layer<=0;time_index<=0;issue_term<=0;consume_term<=0;issue_channel<=0;issue_kernel<=0;
            output_issue<=0;output_lane<=0;gap_channel<=0;embedding_write<=0;embedding_address<=0;embedding_data<=0;
            result_valid<=0;cycles<=0;mac_issue_cycles<=0;debug_valid<=0;debug_layer<=0;debug_address<=0;debug_data<=0;
        end else begin
            embedding_write<=0;debug_valid<=0;
            if(result_valid && result_ready) result_valid<=0;
            if(state!=IDLE) cycles<=cycles+1'b1;
            case(state)
                IDLE:if(start && ready) begin
                    layer<=0;time_index<=0;cycles<=0;mac_issue_cycles<=0;state<=BEGIN_DOT;
                end
                BEGIN_DOT:begin
                    issue_term<=0;consume_term<=0;issue_channel<=0;issue_kernel<=0;output_issue<=0;output_lane<=0;
                    state<=PRIME;
                end
                PRIME:begin issue_term<=1;issue_kernel<=1;state<=RUN;end
                RUN:begin
                    mac_issue_cycles<=mac_issue_cycles+1'b1;
                    if(consume_term==terms-1) state<=OUT_PRIME;
                    else begin
                        consume_term<=consume_term+1'b1;
                        if(issue_term<terms-1) begin
                            issue_term<=issue_term+1'b1;
                            if(issue_kernel==(layer==0 ? 4 : 2)) begin issue_kernel<=0;issue_channel<=issue_channel+1'b1;end
                            else issue_kernel<=issue_kernel+1'b1;
                        end
                    end
                end
                OUT_PRIME:begin output_issue<=1;state<=OUT_RUN;end
                OUT_RUN:begin
                    debug_valid<=1;debug_layer<=layer;
                    debug_address<=(output_lane<<(layer==0 ? 8 : layer==1 ? 7 : 6))+time_index;
                    debug_data<=quantized;
                    if(layer==0 && time_index[0]) a1[{output_lane[3:0],time_index[7:1]}]<=
                        quantized>pool_previous[output_lane] ? quantized : pool_previous[output_lane];
                    // Pool2 writes the recycled a0 in its single write port above.
                    if(output_lane==channels-1) begin
                        if(time_index==width-1) begin
                            time_index<=0;
                            if(layer==2) begin gap_channel<=0;state<=GAP;end
                            else begin layer<=layer+1'b1;state<=BEGIN_DOT;end
                        end else begin time_index<=time_index+1'b1;state<=BEGIN_DOT;end
                    end else begin
                        output_lane<=output_lane+1'b1;
                        if(output_issue<channels-1) output_issue<=output_issue+1'b1;
                    end
                end
                GAP:begin
                    embedding_write<=1;embedding_address<=gap_channel;embedding_data<=(gap_sum[gap_channel]+16'd32)>>6;
                    if(gap_channel==31) begin result_valid<=1;state<=IDLE;end
                    else gap_channel<=gap_channel+1'b1;
                end
                default:state<=IDLE;
            endcase
        end
    end
endmodule
`default_nettype wire
