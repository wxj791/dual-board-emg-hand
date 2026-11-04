`timescale 1ns/1ps
`default_nettype none
// Two parallel causal envelopes per channel. Q8 signed24 filtered samples ->
// abs rounded Q4 -> EMA alpha1/16 and1/64. No DSP, divide, or label input.
module emg_envelope_bank(
    input wire clk,input wire rst_n,input wire sample_valid,
    input wire [191:0] filtered_q8,
    output reg envelope_valid,output wire [319:0] envelopes_q4
);
    function [19:0] magnitude_q4;
        input signed [23:0] value;
        reg [24:0] mag;
        begin
            mag=value<0 ? -$signed({value[23],value}) : $signed({1'b0,value});
            magnitude_q4=(mag+25'd8)>>4;
        end
    endfunction
    function [19:0] update;
        input [19:0] previous,target;
        input slow;
        reg signed [20:0] delta;
        reg [20:0] mag,step;
        begin
            delta=$signed({1'b0,target})-$signed({1'b0,previous});
            mag=delta<0 ? -delta : delta;
            step=slow ? (mag+21'd32)>>6 : (mag+21'd8)>>4;
            update=delta<0 ? previous-step : previous+step;
        end
    endfunction
    genvar c;
    generate for(c=0;c<8;c=c+1) begin:channel
        reg [19:0] fast_state,slow_state;
        wire [19:0] target=magnitude_q4($signed(filtered_q8[c*24 +:24]));
        assign envelopes_q4[c*20 +:20]=fast_state;
        assign envelopes_q4[(8+c)*20 +:20]=slow_state;
        always @(posedge clk or negedge rst_n) begin
            if(!rst_n) begin fast_state<=0;slow_state<=0;end
            else if(sample_valid) begin
                fast_state<=update(fast_state,target,1'b0);
                slow_state<=update(slow_state,target,1'b1);
            end
        end
    end endgenerate
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) envelope_valid<=0;
        else envelope_valid<=sample_valid;
    end
endmodule
`default_nettype wire
