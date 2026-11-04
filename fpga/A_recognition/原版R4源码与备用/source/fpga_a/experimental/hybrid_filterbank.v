`timescale 1ns/1ps
`default_nettype none
// Shared Q8/Q14 biquad engine, covering main frontend AND three subbands.
// FRONT=0 precision, 1 emg, 2 clean. Coefficients generated independently by
// scripts/verify_hybrid_filterbank.py from the trained package contract.
// RAM state avoids clearing/multiplexing hundreds of FF state words.
module hybrid_filterbank #(
    parameter integer FRONT=2,
    parameter integer MAIN=(FRONT==2 ? 6 : 3),
    parameter integer STAGES=MAIN+3
) (
    input wire clk,input wire rst_n,
    input wire sample_valid,output wire sample_ready,input wire [191:0] sample_data,
    output reg filtered_valid,input wire filtered_ready,
    output reg [767:0] filtered_data,output reg [31:0] saturation_count
);
    localparam IDLE=0,PREP=1,WAIT_RAM=2,R0=3,R1=4,R2=5,R3=6,
               MAC=7,QUANT=8,W0=9,W1=10,W2=11,W3=12,ADVANCE=13;
    reg [3:0] state;
    reg [2:0] channel;
    reg [3:0] stage;
    reg [2:0] term;
    reg [191:0] held;
    reg signed [23:0] current_x,main_value,x1,x2,y1,y2,section_value;
    reg signed [47:0] accumulator;
    reg [8:0] ram_address;
    reg signed [23:0] ram_q;
    reg signed [23:0] history[0:511];
    reg [8*STAGES-1:0] live;
    reg state_live;
    // Explicit bounded shift/add addressing prevents the tool from spending
    // a DSP on a wide multiply used only for a bit/array index.
    wire [6:0] state_index=(STAGES==9) ?
        ({channel,3'b000}+{4'b0000,channel}+{3'b000,stage}) :
        ({channel,2'b00}+{channel,1'b0}+{3'b000,stage});
    wire [8:0] base={state_index,2'b00};
    wire [1:0] band_index=stage-MAIN+1;
    wire [4:0] output_lane={band_index,channel};
    wire [9:0] output_bit={output_lane,4'b0000}+{output_lane,3'b000};
    wire [7:0] channel_bit={channel,4'b0000}+{channel,3'b000};
    wire [2:0] next_channel=channel+1'b1;
    wire [7:0] next_channel_bit={next_channel,4'b0000}+{next_channel,3'b000};
    reg write_enable;
    reg [8:0] write_address;
    reg signed [23:0] write_data;
    reg signed [23:0] operand;
    wire signed [15:0] coefficient;
    wire signed [39:0] product=operand*coefficient;
    reg signed [47:0] rounded;
    reg signed [23:0] quantized;

    // Dedicated synchronous read/write processes infer the state BRAM.
    // Reads and writes to the same address are never consumed by the FSM.
    always @(posedge clk) begin
        ram_q<=history[ram_address];
        if(write_enable) history[write_address]<=write_data;
    end
    assign sample_ready=(state==IDLE) && (!filtered_valid || filtered_ready);
    hybrid_filter_coefficients #(.FRONT(FRONT)) rom(stage,term,coefficient);
    always @* begin
        case(term)
            0:operand=current_x;
            1:operand=x1;
            2:operand=x2;
            3:operand=y1;
            default:operand=y2;
        endcase
        rounded=(accumulator>=0) ? ((accumulator+48'sd8192)>>>14) : -(((-accumulator)+48'sd8192)>>>14);
        if(rounded>48'sd8388607) quantized=24'sh7fffff;
        else if(rounded < -48'sd8388608) quantized=24'sh800000;
        else quantized=rounded[23:0];
        write_enable=1'b1;write_address=base;write_data=current_x;
        case(state)
            W0:begin write_address=base;write_data=current_x;end
            W1:begin write_address=base+1;write_data=x1;end
            W2:begin write_address=base+2;write_data=section_value;end
            W3:begin write_address=base+3;write_data=y1;end
            default:write_enable=1'b0;
        endcase
    end
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            state<=IDLE;channel<=0;stage<=0;term<=0;held<=0;
            current_x<=0;main_value<=0;x1<=0;x2<=0;y1<=0;y2<=0;section_value<=0;
            accumulator<=0;ram_address<=0;live<=0;state_live<=0;
            filtered_valid<=0;filtered_data<=0;saturation_count<=0;
        end else begin
            if(filtered_valid && filtered_ready) filtered_valid<=0;
            case(state)
                IDLE:if(sample_valid && sample_ready) begin
                    held<=sample_data;current_x<=$signed(sample_data[23:0]);
                    channel<=0;stage<=0;state<=PREP;
                end
                PREP:begin
                    ram_address<=base;state_live<=live[state_index];
                    accumulator<=0;term<=0;state<=WAIT_RAM;
                end
                WAIT_RAM:begin ram_address<=base+1;state<=R0;end
                R0:begin x1<=state_live ? ram_q : 24'sd0;ram_address<=base+2;state<=R1;end
                R1:begin x2<=state_live ? ram_q : 24'sd0;ram_address<=base+3;state<=R2;end
                R2:begin y1<=state_live ? ram_q : 24'sd0;state<=R3;end
                R3:begin y2<=state_live ? ram_q : 24'sd0;state<=MAC;end
                MAC:begin
                    accumulator<=accumulator+{{8{product[39]}},product};
                    if(term==4) state<=QUANT;
                    else term<=term+1'b1;
                end
                QUANT:begin
                    section_value<=quantized;
                    if(rounded>48'sd8388607 || rounded < -48'sd8388608)
                        saturation_count<=saturation_count+1'b1;
                    state<=W0;
                end
                W0:state<=W1;
                W1:state<=W2;
                W2:state<=W3;
                W3:begin live[state_index]<=1;state<=ADVANCE;end
                ADVANCE:begin
                    if(stage==MAIN-1) begin
                        main_value<=section_value;
                        filtered_data[channel_bit +: 24]<=section_value;
                    end
                    if(stage>=MAIN)
                        filtered_data[output_bit +: 24]<=section_value;
                    if(stage==STAGES-1) begin
                        stage<=0;
                        if(channel==7) begin filtered_valid<=1;state<=IDLE;end
                        else begin
                            channel<=channel+1'b1;
                            current_x<=$signed(held[next_channel_bit +: 24]);state<=PREP;
                        end
                    end else begin
                        stage<=stage+1'b1;
                        current_x<=(stage<MAIN) ? section_value : main_value;
                        state<=PREP;
                    end
                end
                default:state<=IDLE;
            endcase
        end
    end
endmodule
`default_nettype wire
