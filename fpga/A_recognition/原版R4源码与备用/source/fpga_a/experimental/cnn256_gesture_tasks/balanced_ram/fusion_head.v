`timescale 1ns/1ps
`default_nettype none
// Shared 17x16 head: frozen statistics + learned temporal residual.
// CNN scores are aligned and added BEFORE argmax, rest detection and EMA.
module fusion_head #(
    parameter integer CLASSES=7,parameter integer EXPORT_SCORES=0
) (
    input wire clk,input wire rst_n,input wire [15:0] active_classes,
    input wire feature_write,input wire [8:0] feature_address,input wire signed [15:0] feature_data,
    input wire embedding_write,input wire [4:0] embedding_address,input wire signed [7:0] embedding_data,
    input wire start,output wire ready,input wire cnn_done,
    output reg result_valid,input wire result_ready,
    output reg [15:0] raw_class,output reg [15:0] stable_class,output reg [15:0] cnn_class,
    output reg [15:0] action_id,output reg [8:0] margin_q8,
    output wire [CLASSES*48-1:0] baseline_scores,output wire [CLASSES*48-1:0] learned_scores,
    output reg [31:0] cycles,
    input wire parameter_write,input wire [2:0] parameter_bank,
    input wire [31:0] parameter_address,input wire [31:0] parameter_data
);
    localparam IDLE=0,INIT=1,PRIME=2,MAC=3,SCAN_INIT=4,SCAN=5,SMOOTH=6,
        STABLE_INIT=7,STABLE=8,RATIO_INIT=9,RATIO=10,WAIT_CNN=11,CNN_SCAN_INIT=12,CNN_SCAN=13,FUSE=14,COMPLETE=15;
    reg [3:0] state;
    reg learned;
    localparam integer CLASS_BITS=$clog2(CLASSES);
    reg [CLASS_BITS-1:0] head_class,scan,best;
    reg [8:0] issue_index,consume_index;
    reg signed [47:0] accumulator,maximum,second;
    reg [47:0] max_absolute;
    reg decision_live;
    wire signed [47:0] score_q,decision_q;
    wire signed [23:0] cnn_q;
    wire signed [47:0] decision_current=decision_live ? decision_q : 48'sd0;
    wire [CLASSES*48-1:0] score_debug;
    wire [CLASSES*24-1:0] cnn_debug;
    (* ram_style="bram" *) reg signed [15:0] features[0:511],offsets[0:511];
    localparam integer BANKS=(CLASSES*304+2047)/2048;
    wire signed [15:0] weight_q;
    wire [15:0] bank_q[0:BANKS-1];
    reg [$clog2(BANKS+1)-1:0] weight_select;
    assign weight_q=bank_q[weight_select];
    genvar bank;
    generate for(bank=0;bank<BANKS;bank=bank+1) begin:linear_bank
        localparam integer USED=(bank==BANKS-1) ? CLASSES*304-bank*2048 : 2048;
        localparam integer DEPTH=bank<14 ? 2048 : (1<<$clog2(USED));
        localparam integer ADDR_BITS=$clog2(DEPTH);
        (* ram_style=(bank<14 ? "bram_32k" : "bram") *) reg [15:0] memory[0:DEPTH-1];
        reg [15:0] q;
        initial begin
            if(bank==0) $readmemh("linear_bank0.hex",memory);
            if(bank==1) $readmemh("linear_bank1.hex",memory);
            if(bank==2) $readmemh("linear_bank2.hex",memory);
            if(bank==3) $readmemh("linear_bank3.hex",memory);
            if(bank==4) $readmemh("linear_bank4.hex",memory);
            if(bank==5) $readmemh("linear_bank5.hex",memory);
            if(bank==6) $readmemh("linear_bank6.hex",memory);
            if(bank==7) $readmemh("linear_bank7.hex",memory);
            if(bank==8) $readmemh("linear_bank8.hex",memory);
            if(bank==9) $readmemh("linear_bank9.hex",memory);
            if(bank==10) $readmemh("linear_bank10.hex",memory);
            if(bank==11) $readmemh("linear_bank11.hex",memory);
            if(bank==12) $readmemh("linear_bank12.hex",memory);
            if(bank==13) $readmemh("linear_bank13.hex",memory);
            if(bank==14) $readmemh("linear_bank14.hex",memory);
            if(bank==15) $readmemh("linear_bank15.hex",memory);
            if(bank==16) $readmemh("linear_bank16.hex",memory);
            if(bank==17) $readmemh("linear_bank17.hex",memory);
            if(bank==18) $readmemh("linear_bank18.hex",memory);
            if(bank==19) $readmemh("linear_bank19.hex",memory);
            if(bank==20) $readmemh("linear_bank20.hex",memory);
            if(bank==21) $readmemh("linear_bank21.hex",memory);
            if(bank==22) $readmemh("linear_bank22.hex",memory);
            if(bank==23) $readmemh("linear_bank23.hex",memory);
            if(bank==24) $readmemh("linear_bank24.hex",memory);
            if(bank==25) $readmemh("linear_bank25.hex",memory);
            if(bank==26) $readmemh("linear_bank26.hex",memory);
            if(bank==27) $readmemh("linear_bank27.hex",memory);
            if(bank==28) $readmemh("linear_bank28.hex",memory);
            if(bank==29) $readmemh("linear_bank29.hex",memory);
            if(bank==30) $readmemh("linear_bank30.hex",memory);
            if(bank==31) $readmemh("linear_bank31.hex",memory);
        end
        always @(posedge clk) begin
            q<=memory[weight_address[ADDR_BITS-1:0]];
            if(parameter_write && parameter_bank==0 && parameter_address[31:11]==bank)
                memory[parameter_address[ADDR_BITS-1:0]]<=parameter_data[15:0];
        end
        assign bank_q[bank]=q;
    end endgenerate
    reg signed [7:0] embedding[0:31];
    (* ram_style="bram" *) reg signed [7:0] cnn_weights[0:CLASSES*32-1];
    reg signed [47:0] bias[0:CLASSES-1];
    reg signed [31:0] cnn_bias[0:CLASSES-1];reg [7:0] cnn_shift[0:CLASSES-1];
    reg [15:0] action_map[0:CLASSES-1];
    reg signed [15:0] feature_q,offset_q;
    reg signed [7:0] embedding_q,cnn_weight_q;
    wire [31:0] weight_address=(head_class<<8)+(head_class<<5)+(head_class<<4)+issue_index;
    wire [CLASS_BITS+4:0] cnn_weight_address={head_class,issue_index[4:0]};
    wire signed [16:0] operand=learned ? {{9{embedding_q[7]}},embedding_q} :
        $signed({feature_q[15],feature_q})-$signed({offset_q[15],offset_q});
    wire signed [15:0] coefficient=learned ? {{8{cnn_weight_q[7]}},cnn_weight_q} : weight_q;
    wire signed [32:0] product=operand*coefficient;
    wire signed [47:0] dot_next=accumulator+{{15{product[32]}},product};
    reg cnn_completed;
    // Q8 CNN logits -> Q23 statistical scores. Fixed wiring saves a barrel
    // shifter; UART protocol F4 rejects a different scale for this build.
    localparam integer RESIDUAL_LEFT_SHIFT=15;
    reg [31:0] gate_memory[0:0];reg [31:0] fusion_threshold;
    reg strong_statistics;
    initial begin $readmemh("fusion_gate.hex",gate_memory);fusion_threshold=gate_memory[0];end
    always @(posedge clk) if(parameter_write && parameter_bank==4 && parameter_address==153)
        fusion_threshold<=parameter_data;
    wire signed [47:0] correction_wide={{24{cnn_q[23]}},cnn_q};
    // Q23 alignment already appends15 zeros: dividing by4 is exact wiring,
    // including negative corrections; a second rounding datapath is needless.
    wire signed [47:0] aligned_correction=strong_statistics ?
        correction_wide<<<(RESIDUAL_LEFT_SHIFT-2) : correction_wide<<<RESIDUAL_LEFT_SHIFT;
    reg signed [48:0] delta;
    reg [48:0] delta_magnitude,delta_step;
    reg signed [47:0] updated_decision;
    reg [48:0] remainder,doubled;
    reg [47:0] divisor;
    reg [7:0] quotient;
    reg [2:0] ratio_bit;
    wire [47:0] current_absolute=decision_q<0 ? -decision_q : decision_q;
    wire [48:0] gap_value=$signed({maximum[47],maximum})-$signed({second[47],second});
    function signed [47:0] rounded_shift;
        input signed [47:0] value;input [7:0] shift;
        reg [48:0] magnitude,rounded;
        begin
            magnitude=value<0 ? -$signed({value[47],value}) : $signed({1'b0,value});
            rounded=shift==0 ? magnitude : (magnitude+(49'd1<<(shift[4:0]-1'b1)))>>shift[4:0];
            rounded_shift=value<0 ? -$signed(rounded[47:0]) : $signed(rounded[47:0]);
        end
    endfunction
    initial begin

        $readmemh("linear_offsets.hex",offsets,0,303);$readmemh("linear_bias.hex",bias);
        $readmemh("cnn_head_weights.hex",cnn_weights,0,CLASSES*32-1);$readmemh("cnn_head_bias.hex",cnn_bias);
        $readmemh("cnn_head_shift.hex",cnn_shift);$readmemh("action_map.hex",action_map);
    end
    always @(posedge clk) begin
        if(feature_write) features[feature_address]<=feature_data;
        if(embedding_write) embedding[embedding_address]<=embedding_data;
        feature_q<=features[issue_index];offset_q<=offsets[issue_index];
        weight_select<=weight_address>>11;
        embedding_q<=embedding[issue_index[4:0]];
        cnn_weight_q<=cnn_weights[cnn_weight_address];
        if(parameter_write) begin
            case(parameter_bank)
                1:if(parameter_address<304) offsets[parameter_address[8:0]]<=parameter_data[15:0];
                2:if(parameter_address<CLASSES*2) begin
                    if(parameter_address[0]) bias[parameter_address[CLASS_BITS:1]][47:32]<=parameter_data[15:0];
                    else bias[parameter_address[CLASS_BITS:1]][31:0]<=parameter_data;
                end
                5:if(parameter_address<CLASSES*32) cnn_weights[parameter_address[CLASS_BITS+4:0]]<=parameter_data[7:0];
                6:if(parameter_address<CLASSES*2) begin
                    if(parameter_address[0]) cnn_shift[parameter_address[CLASS_BITS:1]]<=parameter_data[7:0];
                    else cnn_bias[parameter_address[CLASS_BITS:1]]<=parameter_data;
                end
                7:if(parameter_address<CLASSES) action_map[parameter_address]<=parameter_data[15:0];
            endcase
        end
    end
    always @* begin
        delta=$signed({score_q[47],score_q})-$signed({decision_current[47],decision_current});
        delta_magnitude=delta<0 ? -delta : delta;
        delta_step=(delta_magnitude+49'd2)>>2;
        updated_decision=raw_class==0 ? score_q :
            delta<0 ? decision_current-delta_step : decision_current+delta_step;
        doubled=remainder<<1;
    end
    genvar g;
    generate if(EXPORT_SCORES) begin:debug_scores
        for(g=0;g<CLASSES;g=g+1) begin:score_outputs
            assign baseline_scores[g*48 +:48]=score_debug[g*48 +:48];
            assign learned_scores[g*48 +:48]={{24{cnn_debug[g*24+23]}},cnn_debug[g*24 +:24]};
        end
    end else begin
        assign baseline_scores=0;assign learned_scores=0;
    end endgenerate
    assign ready=state==IDLE && !result_valid;
    integer i;
    // Prefetch the next class into synchronous scratch BRAM. All scan
    // states keep their original one-class-per-cycle schedule, including the
    // last-class-to-zero transitions before FUSE/SMOOTH/STABLE.
    wire advancing_scan=state==CNN_SCAN || state==FUSE || state==SCAN || state==SMOOTH || state==STABLE;
    wire [CLASS_BITS-1:0] scan_read=advancing_scan && scan!=active_classes-1 ? scan+1'b1 : {CLASS_BITS{1'b0}};
    v2_score_ram #(.WIDTH(48),.DEPTH(CLASSES),.DEBUG(EXPORT_SCORES)) score_store(
        clk,(state==MAC && !learned && consume_index==303) || state==FUSE,
        state==FUSE ? scan : head_class,state==FUSE ? score_q+aligned_correction : dot_next,
        scan_read,score_q,score_debug);
    v2_score_ram #(.WIDTH(24),.DEPTH(CLASSES),.DEBUG(EXPORT_SCORES)) cnn_store(
        clk,state==MAC && learned && consume_index==31,head_class,
        rounded_shift(dot_next,cnn_shift[head_class]),scan_read,cnn_q,cnn_debug);
    v2_score_ram #(.WIDTH(48),.DEPTH(CLASSES)) decision_store(
        clk,state==SMOOTH,scan,updated_decision,scan_read,decision_q,/*debug*/);
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=IDLE;learned<=0;head_class<=0;scan<=0;best<=0;issue_index<=0;consume_index<=0;
            accumulator<=0;maximum<=0;second<=0;max_absolute<=0;cnn_completed<=0;
            strong_statistics<=0;
            result_valid<=0;raw_class<=0;stable_class<=0;cnn_class<=0;action_id<=0;margin_q8<=0;cycles<=0;
            remainder<=0;divisor<=1;quotient<=0;ratio_bit<=0;
            decision_live<=0;
        end else begin
            if(result_valid && result_ready) result_valid<=0;
            if(cnn_done) cnn_completed<=1;
            if(state!=IDLE) cycles<=cycles+1'b1;
            case(state)
                IDLE:if(start && ready) begin
                    learned<=0;head_class<=0;cycles<=0;cnn_completed<=0;state<=INIT;
                    maximum<=48'sh800000000000;second<=48'sh800000000000;
                end
                INIT:begin
                    issue_index<=0;consume_index<=0;
                    accumulator<=learned ? {{16{cnn_bias[head_class][31]}},cnn_bias[head_class]} : bias[head_class];
                    state<=PRIME;
                end
                PRIME:begin issue_index<=1;state<=MAC;end
                MAC:begin
                    accumulator<=dot_next;
                    if(consume_index==(learned ? 31 : 303)) begin
                        if(!learned) begin
                            if(dot_next>maximum) begin second<=maximum;maximum<=dot_next;end
                            else if(dot_next>second) second<=dot_next;
                        end

                        if(head_class==active_classes-1) state<=learned ? CNN_SCAN_INIT : WAIT_CNN;
                        else begin head_class<=head_class+1'b1;state<=INIT;end
                    end else begin
                        consume_index<=consume_index+1'b1;
                        if(issue_index<(learned ? 31 : 303)) issue_index<=issue_index+1'b1;
                    end
                end
                SCAN_INIT:begin scan<=0;best<=0;maximum<=48'sh800000000000;state<=SCAN;end
                SCAN:begin
                    if(score_q>maximum) begin maximum<=score_q;best<=scan;end
                    if(scan==active_classes-1) begin
                        raw_class<=score_q>maximum ? scan : best;scan<=0;state<=SMOOTH;
                    end else scan<=scan+1'b1;
                end
                SMOOTH:begin

                    if(scan==active_classes-1) begin decision_live<=1;state<=STABLE_INIT;end else scan<=scan+1'b1;
                end
                STABLE_INIT:begin
                    scan<=0;best<=0;maximum<=48'sh800000000000;second<=48'sh800000000000;max_absolute<=1;state<=STABLE;
                end
                STABLE:begin
                    if(decision_q>maximum) begin second<=maximum;maximum<=decision_q;best<=scan;end
                    else if(decision_q>second) second<=decision_q;
                    if(current_absolute>max_absolute) max_absolute<=current_absolute;
                    if(scan==active_classes-1) state<=RATIO_INIT;else scan<=scan+1'b1;
                end
                RATIO_INIT:begin
                    stable_class<=best;action_id<=action_map[best];
                    if(gap_value>={1'b0,max_absolute}) begin margin_q8<=256;state<=COMPLETE;end
                    else begin remainder<=gap_value;divisor<=max_absolute;quotient<=0;ratio_bit<=0;state<=RATIO;end
                end
                RATIO:begin
                    if(doubled>={1'b0,divisor}) begin remainder<=doubled-divisor;quotient<={quotient[6:0],1'b1};end
                    else begin remainder<=doubled;quotient<={quotient[6:0],1'b0};end
                    if(ratio_bit==7) begin margin_q8<={1'b0,quotient[6:0],(doubled>={1'b0,divisor})};state<=COMPLETE;end
                    else ratio_bit<=ratio_bit+1'b1;
                end
                WAIT_CNN:if(cnn_completed || cnn_done) begin
                    strong_statistics<=gap_value>={17'b0,fusion_threshold};
                    learned<=1;head_class<=0;state<=INIT;
                end
                CNN_SCAN_INIT:begin scan<=0;best<=0;maximum<=48'sh800000000000;state<=CNN_SCAN;end
                CNN_SCAN:begin
                    if(cnn_q>maximum) begin maximum<=cnn_q;best<=scan;end
                    if(scan==active_classes-1) begin
                        cnn_class<=cnn_q>maximum ? scan : best;scan<=0;state<=FUSE;
                    end else scan<=scan+1'b1;
                end
                FUSE:begin

                    if(scan==active_classes-1) state<=SCAN_INIT;else scan<=scan+1'b1;
                end
                COMPLETE:begin result_valid<=1;state<=IDLE;end
                default:state<=IDLE;
            endcase
        end
    end
endmodule
// Factory-independent scratch state; control validity prevents unwritten reads.
module v2_score_ram #(parameter integer WIDTH=48,DEPTH=7,DEBUG=0)(
 input wire clk,input wire we,input wire [$clog2(DEPTH)-1:0] wa,
 input wire [WIDTH-1:0] wd,input wire [$clog2(DEPTH)-1:0] ra,
 output reg [WIDTH-1:0] rd,output wire [DEPTH*WIDTH-1:0] debug_data
);
 (* ram_style="bram" *) reg [WIDTH-1:0] memory[0:DEPTH-1];
 always @(posedge clk) begin
  if(we) memory[wa]<=wd;
  rd<=memory[ra];
 end
 // Array-wide trace ports must be removed by preprocessing, before TD's
 // RAM extraction pass. A constant-false generate still prevents extraction.
`ifdef A_V2_TRACE_SCORES
 genvar n;
 generate for(n=0;n<DEPTH;n=n+1) begin:debug_entry
  assign debug_data[n*WIDTH +:WIDTH]=memory[n];
 end endgenerate
`else
 assign debug_data=0;
`endif
endmodule
`default_nettype wire
