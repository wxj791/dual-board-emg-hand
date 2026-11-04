`timescale 1ns/1ps
`default_nettype none
// Exact clean/256/stride32 frontend features used by parallel_fusion_v1.
// Sliding sufficient statistics avoid rescanning 4*8*256 samples per window.
// Two shared 21x21 products update new and expired energies/cross energies.
module fusion_features (
    input wire clk,input wire rst_n,
    input wire sample_valid,output wire sample_ready,input wire [767:0] filtered_q8,
    output reg feature_write,output reg [8:0] feature_address,output reg signed [15:0] feature_data,
    output reg wave_write,output reg [10:0] wave_address,output reg signed [20:0] wave_data,
    output reg window_valid,input wire window_ready,
    output reg [31:0] accepted_samples,output reg [31:0] window_end,
    output reg [31:0] processing_cycles,
    output reg telemetry_valid,output reg [191:0] telemetry_filtered,
    output reg [191:0] telemetry_fast,output reg [191:0] telemetry_slow
);
    localparam IDLE=0,ENV=1,RADDR=2,RWAIT=3,ROLD=4,HWAIT=5,RHALF=6,
        BWAIT=7,RBOUND1=8,DWAIT=9,RBOUND4=10,ENERGY=11,HALF=12,MAIN=13,
        WRITE=14,CWAIT=15,CMAC=16,FINISH=17,LOGE=18,STATS=19,
        EA=20,EW=21,EB=22,EX=23,EV=24,ER=25,CA=26,CW=27,CV=28,
        WA=29,WW=30,WV=31,DONE=32,WAIT_SLOT=33,
        ENV_WAIT=34,ENV_ADD=35,MAV_WAIT=36,MAV_ADD=37,WL_WAIT=38,WL_ADD=39,
        LAG_WAIT=40,LAG_ADD=41,LOG_ADDR=42,LOG_WAIT=43,
        STAT_ADDR=44,STAT_WAIT=45,STAT_HALF=46,STAT_HALF_WAIT=47,
        ENV_UPDATE=48,ENV_PREFIX=49,CAPTURE=50;
    reg [5:0] state;
    reg [4:0] lane;
    reg [3:0] env_lane;
    reg [7:0] write_frame;
    reg [12:0] ring_address;
    reg signed [20:0] ring_q;
    (* ram_style="bram_32k" *) reg signed [20:0] ring[0:8191];
    reg signed [20:0] incoming[0:31],expired[0:31];
    // The upstream biquad needs >32 clocks before modifying its next output.
    // Consume the 32 lanes directly during CAPTURE instead of duplicating the
    // entire 768-bit shift register. No arithmetic or state timing changes.
    wire [23:0] capture_lane=filtered_q8[lane*24 +:24];
    wire [4:0] incoming_read_a=state==ENV_UPDATE ? {2'b0,env_lane[2:0]} : state==CMAC ? pair_lane_a : lane;
    wire signed [20:0] incoming_a=incoming[incoming_read_a];
    wire signed [20:0] incoming_b=incoming[pair_lane_b];
    reg signed [20:0] old256,old128,bound1,bound4;
    reg signed [20:0] previous1[0:7],previous2[0:7],previous3[0:7],previous4[0:7];
    // Fixed-depth state RAM, lazily initialized on first accepted sample.
    // 0..31 energy;32..63 recent half energy;64..71 MAV;72..79 WL;
    // 80..87 lag4;96..111 cumulative envelope sums.
    (* ram_style="bram" *) reg [47:0] statistics[0:255];
    reg [7:0] statistics_address,statistics_write_address;
    reg [47:0] statistics_q,statistics_write_data,energy_before,half_before,encoded_energy;
    reg statistics_write;
    reg [47:0] statistics_base,statistics_addend,statistics_subtrahend;
    reg [41:0] new_square;
    reg [6:0] cross_index;
    reg [1:0] band;
    reg [2:0] pair_a,pair_b;
    (* ram_style="bram" *) reg signed [47:0] cross_energy[0:111];
    reg signed [47:0] cross_q;
    reg signed [15:0] log_energy[0:31];
    (* ram_style="bram" *) reg [39:0] prefix_history[0:255];
    reg [7:0] prefix_address;
    reg [39:0] prefix_q,prefix_first;
    reg [2:0] env_block,env_channel;
    reg signed [15:0] env_codes[0:7];
    reg signed [19:0] env_total;
    reg [5:0] stat_index;
    reg [2:0] wave_channel;
    reg [7:0] wave_time;
    wire accepted=sample_valid && sample_ready;
    reg [19:0] envelope_updated;
    wire [19:0] envelope_target=magnitude21(incoming_a);
    wire signed [20:0] envelope_delta=$signed({1'b0,envelope_target})-
        $signed({1'b0,(accepted_samples==0 ? 20'd0 : statistics_q[19:0])});
    wire [20:0] envelope_magnitude=envelope_delta<0 ? -envelope_delta : envelope_delta;
    wire [20:0] envelope_step=env_lane[3] ? (envelope_magnitude+21'd32)>>6 : (envelope_magnitude+21'd8)>>4;
    wire [19:0] envelope_previous=accepted_samples==0 ? 20'd0 : statistics_q[19:0];
    wire [19:0] envelope_next=envelope_delta<0 ? envelope_previous-envelope_step : envelope_previous+envelope_step;
    assign sample_ready=(state==IDLE) && !window_valid;
    wire [31:0] next_count=accepted_samples+1'b1;
    wire [3:0] snapshot_slot=next_count[8:5];
    wire [39:0] next_prefix=(accepted_samples==0 ? 40'd0 : statistics_q[39:0])+envelope_updated;
    wire [3:0] prefix_left_slot=window_end[8:5]-4'd8+{env_block[1:0],1'b0};
    wire [3:0] prefix_right_slot=window_end[8:5]-4'd6+{env_block[1:0],1'b0};
    wire [3:0] env_source={env_block[2],env_channel};
    wire [39:0] envelope_sum=prefix_q-prefix_first;
    wire [47:0] envelope_mean={8'b0,((envelope_sum+40'd32)>>6)};
    reg [47:0] log_input;
    wire signed [15:0] log_value;
    function signed [20:0] q4;
        input signed [23:0] x;reg [24:0] magnitude;
        begin
            magnitude=x<0 ? -$signed({x[23],x}) : $signed({1'b0,x});
            q4=x<0 ? -$signed((magnitude+25'd8)>>4) : $signed((magnitude+25'd8)>>4);
        end
    endfunction
    function [21:0] absdiff;
        input signed [20:0] a,b;reg signed [21:0] d;
        begin d={a[20],a}-{b[20],b};absdiff=d<0 ? -d : d;end
    endfunction
    function [20:0] magnitude21;
        input signed [20:0] a;begin magnitude21=a<0 ? -a : a;end
    endfunction
    function signed [15:0] qlog;
        input [47:0] x;reg [47:0] v,mantissa;integer i;reg [5:0] e;
        begin
            v=x==0 ? 48'd1 : x;e=0;
            for(i=0;i<48;i=i+1) if(v[i]) e=i;
            // The leading one lands in bit8; discarding it is exactly the
            // original (v-2**e)*256/2**e mantissa without a 48-bit subtract.
            mantissa=e>=8 ? (v>>(e-6'd8)) : (v<<(6'd8-e));
            qlog={2'b0,e,mantissa[7:0]};
        end
    endfunction
    assign log_value=qlog(log_input);
    reg signed [20:0] multiply_a,multiply_b,multiply_c,multiply_d;
    wire signed [41:0] product_new=multiply_a*multiply_b;
    wire signed [41:0] product_old=multiply_c*multiply_d;
    wire [4:0] pair_lane_a={band,pair_a};
    wire [4:0] pair_lane_b={band,pair_b};
    reg signed [16:0] corr_magnitude;
    reg signed [16:0] energy_log_sum;
    reg signed [15:0] correlation;
    reg signed [48:0] half_difference;
    always @* begin
        multiply_a=incoming_a;multiply_b=incoming_a;
        multiply_c=old256;multiply_d=old256;
        if(state==HALF) begin multiply_a=old128;multiply_b=old128;end
        if(state==CMAC) begin
            multiply_a=incoming_a;multiply_b=incoming_b;
            multiply_c=expired[pair_lane_a];multiply_d=expired[pair_lane_b];
        end
        half_difference=$signed({1'b0,encoded_energy})-($signed({1'b0,statistics_q})<<<1);
        log_input=0;
        if(state==LOGE) log_input=statistics_q;
        else if(state==STATS) begin
            if(stat_index[5:3]==4) log_input=half_difference<0 ? -half_difference : half_difference;
            else log_input=statistics_q;
        end else if(state==EV) log_input=envelope_mean;
        else if(state==CV) log_input=cross_q<0 ? -cross_q : cross_q;
        energy_log_sum=$signed(log_energy[pair_lane_a])+$signed(log_energy[pair_lane_b]);
        corr_magnitude=17'sd2048+$signed(log_value)-(energy_log_sum>>>1);
        if(corr_magnitude<0) correlation=0;
        else if(corr_magnitude>2048) correlation=16'sd2048;
        else correlation=corr_magnitude[15:0];
        if(cross_q<0) correlation=-correlation;
        if(cross_q==0) correlation=0;
        statistics_write=1;statistics_write_address=0;
        statistics_base=accepted_samples==0 ? 48'd0 : statistics_q;
        statistics_addend=0;statistics_subtrahend=0;
        case(state)
            ENV_UPDATE:begin statistics_write_address=8'd112+env_lane;statistics_base={28'b0,envelope_next};end
            ENV_ADD:begin statistics_write_address=8'd96+env_lane;statistics_base={8'b0,next_prefix};end
            ENERGY:begin
                statistics_write_address={3'b0,lane};statistics_base=energy_before;
                statistics_addend={{6{product_new[41]}},product_new};statistics_subtrahend={{6{product_old[41]}},product_old};
            end
            HALF:begin
                statistics_write_address=8'd32+lane;statistics_base=half_before;
                statistics_addend={6'b0,new_square};statistics_subtrahend={{6{product_new[41]}},product_new};
            end
            MAV_ADD:begin
                statistics_write_address=8'd64+lane[2:0];
                statistics_addend=magnitude21(incoming_a);statistics_subtrahend=magnitude21(old256);
            end
            WL_ADD:begin
                statistics_write_address=8'd72+lane[2:0];
                statistics_addend=accepted_samples>=1 ? absdiff(incoming_a,previous1[lane[2:0]]) : 22'd0;
                statistics_subtrahend=accepted_samples>=256 ? absdiff(bound1,old256) : 22'd0;
            end
            LAG_ADD:begin
                statistics_write_address=8'd80+lane[2:0];
                statistics_addend=accepted_samples>=4 ? absdiff(incoming_a,previous4[lane[2:0]]) : 22'd0;
                statistics_subtrahend=accepted_samples>=256 ? absdiff(bound4,old256) : 22'd0;
            end
            default:statistics_write=0;
        endcase
        statistics_write_data=statistics_base+statistics_addend-statistics_subtrahend;
    end
    // History has no bulk reset: WL/lag readers use accepted_samples to
    // exclude unwritten history. After four samples every lane is initialized.
    // This permits distributed RAM instead of resettable flip-flop mux banks.
    always @(posedge clk) if(state==LAG_ADD) begin
        previous4[lane[2:0]]<=previous3[lane[2:0]];previous3[lane[2:0]]<=previous2[lane[2:0]];
        previous2[lane[2:0]]<=previous1[lane[2:0]];previous1[lane[2:0]]<=incoming_a;
    end
    always @(posedge clk) begin
        ring_q<=ring[ring_address];
        if(state==CAPTURE) incoming[lane]<=q4($signed(capture_lane));
        if(state==ROLD) expired[lane]<=accepted_samples>=256 ? ring_q : 21'sd0;
        if(state==WRITE) ring[{lane,write_frame}]<=incoming_a;
        cross_q<=cross_energy[cross_index];
        if(state==CMAC) cross_energy[cross_index]<=
            (accepted_samples==0 ? 48'sd0 : cross_q)+
            {{6{product_new[41]}},product_new}-{{6{product_old[41]}},product_old};
        prefix_q<=prefix_history[prefix_address];
        if(state==ENV_ADD && next_count[4:0]==0)
            prefix_history[{snapshot_slot,env_lane}]<=next_prefix;
        statistics_q<=statistics[statistics_address];
        if(statistics_write) statistics[statistics_write_address]<=statistics_write_data;
    end
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=IDLE;lane<=0;env_lane<=0;write_frame<=0;ring_address<=0;
            old256<=0;old128<=0;bound1<=0;bound4<=0;new_square<=0;
            cross_index<=0;band<=0;pair_a<=0;pair_b<=1;
            prefix_address<=0;prefix_first<=0;env_block<=0;env_channel<=0;env_total<=0;
            stat_index<=0;wave_channel<=0;wave_time<=0;
            statistics_address<=0;energy_before<=0;half_before<=0;encoded_energy<=0;envelope_updated<=0;
            telemetry_valid<=0;telemetry_filtered<=0;telemetry_fast<=0;telemetry_slow<=0;
            feature_write<=0;feature_address<=0;feature_data<=0;
            wave_write<=0;wave_address<=0;wave_data<=0;window_valid<=0;
            accepted_samples<=0;window_end<=0;processing_cycles<=0;
            for(i=0;i<32;i=i+1) log_energy[i]<=0;
            for(i=0;i<8;i=i+1) env_codes[i]<=0;
        end else begin
            feature_write<=0;wave_write<=0;telemetry_valid<=0;
            if(window_valid && window_ready) window_valid<=0;
            if(state!=IDLE) processing_cycles<=processing_cycles+1'b1;
            case(state)
                IDLE:if(accepted) begin
                    telemetry_filtered<=filtered_q8[191:0];lane<=0;env_lane<=0;processing_cycles<=0;state<=CAPTURE;
                end
                CAPTURE:begin
                    if(lane==31) state<=ENV;else lane<=lane+1'b1;
                end
                ENV:begin statistics_address<=8'd112+env_lane;state<=ENV_WAIT;end
                ENV_WAIT:state<=ENV_UPDATE;
                ENV_UPDATE:begin
                    if(env_lane[3]) telemetry_slow[env_lane[2:0]*24 +:24]<={4'b0,envelope_next};
                    else telemetry_fast[env_lane[2:0]*24 +:24]<={4'b0,envelope_next};
                    envelope_updated<=envelope_next;statistics_address<=8'd96+env_lane;state<=ENV_PREFIX;end
                ENV_PREFIX:state<=ENV_ADD;
                ENV_ADD:begin
                    if(env_lane==15) begin telemetry_valid<=1;lane<=0;state<=RADDR;end
                    else begin env_lane<=env_lane+1'b1;state<=ENV;end
                end
                RADDR:begin ring_address<={lane,write_frame};statistics_address<={3'b0,lane};state<=RWAIT;end
                RWAIT:state<=ROLD;
                ROLD:begin
                    old256<=accepted_samples>=256 ? ring_q : 21'sd0;
                    energy_before<=accepted_samples==0 ? 48'd0 : statistics_q;
                    statistics_address<=8'd32+lane;
                    ring_address<={lane,(write_frame-8'd128)};state<=HWAIT;
                end
                HWAIT:state<=RHALF;
                RHALF:begin
                    old128<=accepted_samples>=128 ? ring_q : 21'sd0;
                    half_before<=accepted_samples==0 ? 48'd0 : statistics_q;
                    if(lane<8) begin ring_address<={lane,(write_frame+8'd1)};state<=BWAIT;end
                    else state<=ENERGY;
                end
                BWAIT:state<=RBOUND1;
                RBOUND1:begin bound1<=ring_q;ring_address<={lane,(write_frame+8'd4)};state<=DWAIT;end
                DWAIT:state<=RBOUND4;
                RBOUND4:begin bound4<=ring_q;state<=ENERGY;end
                ENERGY:begin
                    new_square<=product_new;
                    state<=HALF;
                end
                HALF:begin
                    state<=lane<8 ? MAIN : WRITE;
                end
                MAIN:begin statistics_address<=8'd64+lane[2:0];state<=MAV_WAIT;end
                MAV_WAIT:state<=MAV_ADD;
                MAV_ADD:begin statistics_address<=8'd72+lane[2:0];state<=WL_WAIT;end
                WL_WAIT:state<=WL_ADD;
                WL_ADD:begin statistics_address<=8'd80+lane[2:0];state<=LAG_WAIT;end
                LAG_WAIT:state<=LAG_ADD;
                LAG_ADD:begin
                    state<=WRITE;
                end
                WRITE:if(lane==31) begin cross_index<=0;band<=0;pair_a<=0;pair_b<=1;state<=CWAIT;end
                      else begin lane<=lane+1'b1;state<=RADDR;end
                CWAIT:state<=CMAC;
                CMAC:begin
                    if(cross_index==111) state<=FINISH;
                    else begin
                        cross_index<=cross_index+1'b1;state<=CWAIT;
                        if(pair_b==7) begin
                            if(pair_a==6) begin band<=band+1'b1;pair_a<=0;pair_b<=1;end
                            else begin pair_a<=pair_a+1'b1;pair_b<=pair_a+2'd2;end
                        end else pair_b<=pair_b+1'b1;
                    end
                end
                FINISH:begin
                    accepted_samples<=next_count;write_frame<=write_frame+1'b1;
                    if(next_count>=576 && next_count[4:0]==0) begin
                        window_end<=next_count;lane<=0;state<=window_ready ? LOG_ADDR : WAIT_SLOT;
                    end
                    else state<=IDLE;
                end
                WAIT_SLOT:if(window_ready) state<=LOG_ADDR;
                LOG_ADDR:begin statistics_address<={3'b0,lane};state<=LOG_WAIT;end
                LOG_WAIT:state<=LOGE;
                LOGE:begin
                    log_energy[lane]<=log_value;
                    if(lane==31) begin stat_index<=0;state<=STAT_ADDR;end
                    else begin lane<=lane+1'b1;state<=LOG_ADDR;end
                end
                STAT_ADDR:begin
                    case(stat_index[5:3])
                        0:statistics_address<=8'd64+stat_index[2:0];
                        1,4:statistics_address<={5'b0,stat_index[2:0]};
                        2:statistics_address<=8'd72+stat_index[2:0];
                        3:statistics_address<=8'd80+stat_index[2:0];
                        5:statistics_address<=8'd8+stat_index[2:0];
                        6:statistics_address<=8'd16+stat_index[2:0];
                        7:statistics_address<=8'd24+stat_index[2:0];
                    endcase
                    state<=STAT_WAIT;
                end
                STAT_WAIT:state<=stat_index[5:3]==4 ? STAT_HALF : STATS;
                STAT_HALF:begin encoded_energy<=statistics_q;statistics_address<=8'd32+stat_index[2:0];state<=STAT_HALF_WAIT;end
                STAT_HALF_WAIT:state<=STATS;
                STATS:begin
                    feature_write<=1;feature_address<=stat_index;feature_data<=log_value;
                    if(stat_index==63) begin env_block<=0;env_channel<=0;env_total<=0;state<=EA;end
                    else begin stat_index<=stat_index+1'b1;state<=STAT_ADDR;end
                end
                EA:begin prefix_address<={prefix_left_slot,env_source};state<=EW;end
                EW:state<=EB;
                EB:begin prefix_first<=prefix_q;prefix_address<={prefix_right_slot,env_source};state<=EX;end
                EX:state<=EV;
                EV:begin
                    feature_write<=1;feature_address<=9'd64+{env_block,env_channel};feature_data<=log_value;
                    env_codes[env_channel]<=log_value;env_total<=env_total+log_value;
                    if(env_channel==7) begin env_channel<=0;state<=ER;end
                    else begin env_channel<=env_channel+1'b1;state<=EA;end
                end
                ER:begin
                    feature_write<=1;feature_address<=9'd128+{env_block,env_channel};
                    feature_data<=env_codes[env_channel]-(env_total>>>3);
                    if(env_channel==7) begin
                        env_channel<=0;env_total<=0;
                        if(env_block==7) begin cross_index<=0;band<=0;pair_a<=0;pair_b<=1;state<=CA;end
                        else begin env_block<=env_block+1'b1;state<=EA;end
                    end else env_channel<=env_channel+1'b1;
                end
                CA:state<=CW;
                CW:state<=CV;
                CV:begin
                    feature_write<=1;feature_address<=9'd192+cross_index;feature_data<=correlation;
                    if(cross_index==111) begin wave_channel<=0;wave_time<=0;state<=WA;end
                    else begin
                        cross_index<=cross_index+1'b1;state<=CA;
                        if(pair_b==7) begin
                            if(pair_a==6) begin band<=band+1'b1;pair_a<=0;pair_b<=1;end
                            else begin pair_a<=pair_a+1'b1;pair_b<=pair_a+2'd2;end
                        end else pair_b<=pair_b+1'b1;
                    end
                end
                WA:begin ring_address<={2'b0,wave_channel,(write_frame+wave_time)};state<=WW;end
                WW:state<=WV;
                WV:begin
                    wave_write<=1;wave_address<={wave_channel,wave_time};wave_data<=ring_q;
                    if(wave_time==255) begin
                        wave_time<=0;
                        if(wave_channel==7) state<=DONE;
                        else begin wave_channel<=wave_channel+1'b1;state<=WA;end
                    end else begin wave_time<=wave_time+1'b1;state<=WA;end
                end
                DONE:begin window_valid<=1;state<=IDLE;end
                default:state<=IDLE;
            endcase
        end
    end
endmodule
`default_nettype wire
