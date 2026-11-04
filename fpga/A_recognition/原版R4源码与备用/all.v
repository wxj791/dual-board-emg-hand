`timescale 1ns/1ps

module reset_sync_n #(
    parameter integer STAGES = 2
) (
    input  wire clk,
    input  wire async_rst_n,
    output wire sync_rst_n
);
    reg [STAGES-1:0] sync_pipe;

    always @(posedge clk or negedge async_rst_n) begin
        if (!async_rst_n)
            sync_pipe <= {STAGES{1'b0}};
        else
            sync_pipe <= {sync_pipe[STAGES-2:0], 1'b1};
    end

    assign sync_rst_n = sync_pipe[STAGES-1];
endmodule

`default_nettype none

module link_crc16_ccitt (
    input  wire [15:0] crc_in,
    input  wire [7:0]  data_in,
    output reg  [15:0] crc_out
);
    integer bit_index;
    reg [15:0] work;

    always @* begin
        work = crc_in ^ {data_in, 8'h00};
        for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1) begin
            if (work[15])
                work = {work[14:0], 1'b0} ^ 16'h1021;
            else
                work = {work[14:0], 1'b0};
        end
        crc_out = work;
    end
endmodule

`default_nettype wire

`default_nettype none

module link_frame_rx #(
    parameter integer TIMEOUT_CYCLES = 240000
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        byte_valid,
    output wire        byte_ready,
    input  wire [7:0]  byte_data,

    output reg         frame_valid,
    input  wire        frame_ready,
    output reg  [7:0]  frame_type,
    output reg  [15:0] frame_seq,
    output reg  [31:0] frame_timestamp,
    output reg  [10:0] frame_length,
    output wire        payload_valid,
    input  wire        payload_ready,
    output wire [7:0]  payload_data,
    output wire        payload_last,

    output reg         good_frame_pulse,
    output reg  [7:0]  good_frame_type,
    output wire        busy,
    output reg  [4:0]  state_code,
    output reg  [31:0] good_frame_count,
    output reg  [31:0] byte_count,
    output reg  [31:0] crc_error_count,
    output reg  [31:0] sequence_gap_count,
    output reg  [31:0] length_error_count,
    output reg  [31:0] version_error_count,
    output reg  [31:0] timeout_count,
    output reg  [31:0] selftest_error_count,
    output reg  [7:0]  last_error_code
);
    localparam [4:0] ST_SEARCH_A5 = 5'd0;
    localparam [4:0] ST_SEARCH_5A = 5'd1;
    localparam [4:0] ST_VERSION   = 5'd2;
    localparam [4:0] ST_TYPE      = 5'd3;
    localparam [4:0] ST_SEQ0      = 5'd4;
    localparam [4:0] ST_SEQ1      = 5'd5;
    localparam [4:0] ST_LEN0      = 5'd6;
    localparam [4:0] ST_LEN1      = 5'd7;
    localparam [4:0] ST_TS0       = 5'd8;
    localparam [4:0] ST_TS1       = 5'd9;
    localparam [4:0] ST_TS2       = 5'd10;
    localparam [4:0] ST_TS3       = 5'd11;
    localparam [4:0] ST_PAYLOAD   = 5'd12;
    localparam [4:0] ST_CRC0      = 5'd13;
    localparam [4:0] ST_CRC1      = 5'd14;
    localparam [4:0] ST_FRAME_WAIT= 5'd15;
    localparam [4:0] ST_OUTPUT    = 5'd16;

    localparam [7:0] ERR_CRC      = 8'h20;
    localparam [7:0] ERR_SEQ      = 8'h21;
    localparam [7:0] ERR_LENGTH   = 8'h22;
    localparam [7:0] ERR_TIMEOUT  = 8'h23;
    localparam [7:0] ERR_VERSION  = 8'h25;
    localparam [7:0] ERR_SELFTEST = 8'h26;

    reg [7:0] payload_memory [0:1023];
    reg [7:0] parsed_type;
    reg [15:0] parsed_seq;
    reg [15:0] parsed_length;
    reg [31:0] parsed_timestamp;
    reg [10:0] payload_index;
    reg [10:0] output_index;
    reg [7:0] payload_read_data;
    wire [9:0] payload_read_address;
    reg [15:0] crc_value;
    reg [7:0] received_crc_low;
    wire [15:0] crc_next;
    wire byte_take;
    reg [31:0] idle_cycles;
    reg sequence_initialized;
    reg [15:0] expected_sequence;
    reg selftest_bad;
    reg [7:0] selftest_mode;
    reg [7:0] selftest_previous;
    wire [7:0] selftest_prbs_next;

    link_crc16_ccitt crc_step (
        .crc_in(crc_value),
        .data_in(byte_data),
        .crc_out(crc_next)
    );

    assign byte_ready = (state_code != ST_FRAME_WAIT) &&
                        (state_code != ST_OUTPUT);
    assign byte_take = byte_valid && byte_ready;
    assign payload_valid = (state_code == ST_OUTPUT);
    assign payload_data = payload_read_data;
    assign payload_read_address = (payload_valid && payload_ready && !payload_last) ?
                                   output_index[9:0] + 1'b1 : output_index[9:0];
    assign payload_last = (state_code == ST_OUTPUT) &&
                          (output_index == frame_length - 1'b1);
    assign busy = (state_code != ST_SEARCH_A5) || frame_valid;
    assign selftest_prbs_next = {selftest_previous[6:0],
                                 selftest_previous[7] ^ selftest_previous[5] ^
                                 selftest_previous[4] ^ selftest_previous[3]};

    // Synchronous read maps the complete-frame payload store to block RAM.
    // Prefetch the next address on each output transfer to retain one byte/cycle.
    always @(posedge clk)
        payload_read_data <= payload_memory[payload_read_address];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state_code <= ST_SEARCH_A5;
            parsed_type <= 8'h00;
            parsed_seq <= 16'h0000;
            parsed_length <= 16'h0000;
            parsed_timestamp <= 32'h0000_0000;
            payload_index <= 11'd0;
            output_index <= 11'd0;
            crc_value <= 16'hffff;
            received_crc_low <= 8'h00;
            frame_valid <= 1'b0;
            frame_type <= 8'h00;
            frame_seq <= 16'h0000;
            frame_timestamp <= 32'h0000_0000;
            frame_length <= 11'd0;
            good_frame_pulse <= 1'b0;
            good_frame_type <= 8'h00;
            good_frame_count <= 32'h0000_0000;
            byte_count <= 32'h0000_0000;
            crc_error_count <= 32'h0000_0000;
            sequence_gap_count <= 32'h0000_0000;
            length_error_count <= 32'h0000_0000;
            version_error_count <= 32'h0000_0000;
            timeout_count <= 32'h0000_0000;
            selftest_error_count <= 32'h0000_0000;
            last_error_code <= 8'h00;
            idle_cycles <= 32'h0000_0000;
            sequence_initialized <= 1'b0;
            expected_sequence <= 16'h0000;
            selftest_bad <= 1'b0;
            selftest_mode <= 8'h00;
            selftest_previous <= 8'h00;
        end else begin
            good_frame_pulse <= 1'b0;

            if ((state_code == ST_SEARCH_5A ||
                 (state_code >= ST_VERSION && state_code <= ST_CRC1)) &&
                !byte_take) begin
                if (idle_cycles >= TIMEOUT_CYCLES-1) begin
                    state_code <= ST_SEARCH_A5;
                    idle_cycles <= 32'h0000_0000;
                    if (timeout_count != 32'hffff_ffff)
                        timeout_count <= timeout_count + 1'b1;
                    last_error_code <= ERR_TIMEOUT;
                end else begin
                    idle_cycles <= idle_cycles + 1'b1;
                end
            end else begin
                idle_cycles <= 32'h0000_0000;
            end

            if (state_code == ST_FRAME_WAIT && frame_valid && frame_ready) begin
                frame_valid <= 1'b0;
                if (frame_length == 0)
                    state_code <= ST_SEARCH_A5;
                else begin
                    output_index <= 11'd0;
                    state_code <= ST_OUTPUT;
                end
            end else if (state_code == ST_OUTPUT && payload_valid && payload_ready) begin
                if (payload_last) begin
                    state_code <= ST_SEARCH_A5;
                    output_index <= 11'd0;
                end else begin
                    output_index <= output_index + 1'b1;
                end
            end

            if (byte_take) begin
                if (byte_count != 32'hffff_ffff)
                    byte_count <= byte_count + 1'b1;
                case (state_code)
                    ST_SEARCH_A5: begin
                        if (byte_data == 8'ha5)
                            state_code <= ST_SEARCH_5A;
                    end
                    ST_SEARCH_5A: begin
                        if (byte_data == 8'h5a) begin
                            crc_value <= 16'hffff;
                            payload_index <= 11'd0;
                            selftest_bad <= 1'b0;
                            state_code <= ST_VERSION;
                        end else if (byte_data != 8'ha5)
                            state_code <= ST_SEARCH_A5;
                    end
                    ST_VERSION: begin
                        if (byte_data != 8'h01) begin
                            state_code <= ST_SEARCH_A5;
                            if (version_error_count != 32'hffff_ffff)
                                version_error_count <= version_error_count + 1'b1;
                            last_error_code <= ERR_VERSION;
                        end else begin
                            crc_value <= crc_next;
                            state_code <= ST_TYPE;
                        end
                    end
                    ST_TYPE: begin
                        parsed_type <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_SEQ0;
                    end
                    ST_SEQ0: begin
                        parsed_seq[7:0] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_SEQ1;
                    end
                    ST_SEQ1: begin
                        parsed_seq[15:8] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_LEN0;
                    end
                    ST_LEN0: begin
                        parsed_length[7:0] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_LEN1;
                    end
                    ST_LEN1: begin
                        parsed_length[15:8] <= byte_data;
                        if ({byte_data, parsed_length[7:0]} > 16'd1024) begin
                            state_code <= ST_SEARCH_A5;
                            if (length_error_count != 32'hffff_ffff)
                                length_error_count <= length_error_count + 1'b1;
                            last_error_code <= ERR_LENGTH;
                        end else begin
                            crc_value <= crc_next;
                            state_code <= ST_TS0;
                        end
                    end
                    ST_TS0: begin
                        parsed_timestamp[7:0] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_TS1;
                    end
                    ST_TS1: begin
                        parsed_timestamp[15:8] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_TS2;
                    end
                    ST_TS2: begin
                        parsed_timestamp[23:16] <= byte_data;
                        crc_value <= crc_next;
                        state_code <= ST_TS3;
                    end
                    ST_TS3: begin
                        parsed_timestamp[31:24] <= byte_data;
                        crc_value <= crc_next;
                        payload_index <= 11'd0;
                        selftest_bad <= 1'b0;
                        selftest_mode <= 8'h00;
                        selftest_previous <= 8'h00;
                        if (parsed_length == 0)
                            state_code <= ST_CRC0;
                        else
                            state_code <= ST_PAYLOAD;
                    end
                    ST_PAYLOAD: begin
                        payload_memory[payload_index] <= byte_data;
                        crc_value <= crc_next;
                        if (parsed_type == 8'h7e) begin
                            if (payload_index == 0) begin
                                selftest_mode <= byte_data;
                                if (byte_data > 1)
                                    selftest_bad <= 1'b1;
                            end else if (payload_index == 1) begin
                                selftest_previous <= byte_data;
                                if ((selftest_mode == 1) && (byte_data == 0))
                                    selftest_bad <= 1'b1;
                            end else begin
                                if (selftest_mode == 0) begin
                                    if (byte_data != selftest_previous + 1'b1)
                                        selftest_bad <= 1'b1;
                                end else if (selftest_mode == 1) begin
                                    if (byte_data != selftest_prbs_next)
                                        selftest_bad <= 1'b1;
                                end else begin
                                    selftest_bad <= 1'b1;
                                end
                                selftest_previous <= byte_data;
                            end
                        end
                        if (payload_index == parsed_length[10:0] - 1'b1)
                            state_code <= ST_CRC0;
                        else
                            payload_index <= payload_index + 1'b1;
                    end
                    ST_CRC0: begin
                        received_crc_low <= byte_data;
                        state_code <= ST_CRC1;
                    end
                    ST_CRC1: begin
                        if ({byte_data, received_crc_low} != crc_value) begin
                            state_code <= ST_SEARCH_A5;
                            if (crc_error_count != 32'hffff_ffff)
                                crc_error_count <= crc_error_count + 1'b1;
                            last_error_code <= ERR_CRC;
                        end else begin
                            frame_type <= parsed_type;
                            frame_seq <= parsed_seq;
                            frame_timestamp <= parsed_timestamp;
                            frame_length <= parsed_length[10:0];
                            frame_valid <= 1'b1;
                            state_code <= ST_FRAME_WAIT;
                            good_frame_pulse <= 1'b1;
                            good_frame_type <= parsed_type;
                            if (good_frame_count != 32'hffff_ffff)
                                good_frame_count <= good_frame_count + 1'b1;
                            if (sequence_initialized &&
                                (parsed_seq != expected_sequence)) begin
                                if (sequence_gap_count != 32'hffff_ffff)
                                    sequence_gap_count <= sequence_gap_count + 1'b1;
                                last_error_code <= ERR_SEQ;
                            end
                            sequence_initialized <= 1'b1;
                            expected_sequence <= parsed_seq + 1'b1;
                            if ((parsed_type == 8'h7e) && selftest_bad) begin
                                if (selftest_error_count != 32'hffff_ffff)
                                    selftest_error_count <= selftest_error_count + 1'b1;
                                last_error_code <= ERR_SELFTEST;
                            end
                        end
                    end
                    default: state_code <= ST_SEARCH_A5;
                endcase
            end
        end
    end
endmodule

`default_nettype wire

`default_nettype none

module link_frame_tx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        link_fall_tick,
    input  wire        phy_ready,

    input  wire        frame_valid,
    output wire        frame_ready,
    input  wire [7:0]  frame_type,
    input  wire [31:0] frame_timestamp,
    input  wire [10:0] frame_length,
    input  wire        payload_valid,
    output wire        payload_ready,
    input  wire [7:0]  payload_data,

    output reg  [7:0]  phy_data,
    output reg         phy_valid,
    output wire        busy,
    output reg  [4:0]  state_code,
    output reg  [15:0] next_sequence,
    output reg  [31:0] frame_count,
    output reg  [31:0] byte_count,
    output reg  [31:0] max_stall_periods
);
    localparam [4:0] ST_IDLE    = 5'd0;
    localparam [4:0] ST_SYNC0   = 5'd1;
    localparam [4:0] ST_SYNC1   = 5'd2;
    localparam [4:0] ST_VERSION = 5'd3;
    localparam [4:0] ST_TYPE    = 5'd4;
    localparam [4:0] ST_SEQ0    = 5'd5;
    localparam [4:0] ST_SEQ1    = 5'd6;
    localparam [4:0] ST_LEN0    = 5'd7;
    localparam [4:0] ST_LEN1    = 5'd8;
    localparam [4:0] ST_TS0     = 5'd9;
    localparam [4:0] ST_TS1     = 5'd10;
    localparam [4:0] ST_TS2     = 5'd11;
    localparam [4:0] ST_TS3     = 5'd12;
    localparam [4:0] ST_PAYLOAD = 5'd13;
    localparam [4:0] ST_CRC0    = 5'd14;
    localparam [4:0] ST_CRC1    = 5'd15;

    reg descriptor_pending;
    reg [7:0] pending_type;
    reg [31:0] pending_timestamp;
    reg [10:0] pending_length;
    reg [7:0] active_type;
    reg [31:0] active_timestamp;
    reg [10:0] active_length;
    reg [15:0] active_sequence;
    reg [10:0] payload_index;
    reg [15:0] crc_value;
    wire [15:0] crc_next;
    wire byte_take;
    reg [31:0] stall_periods;

    link_crc16_ccitt crc_step (
        .crc_in(crc_value),
        .data_in(phy_data),
        .crc_out(crc_next)
    );

    assign frame_ready = (state_code == ST_IDLE) && !descriptor_pending;
    assign payload_ready = (state_code == ST_PAYLOAD) &&
                           link_fall_tick && phy_ready;
    assign byte_take = link_fall_tick && phy_ready && phy_valid;
    assign busy = (state_code != ST_IDLE) || descriptor_pending;

    always @* begin
        phy_data = 8'h00;
        phy_valid = 1'b1;
        case (state_code)
            ST_IDLE: begin phy_data = 8'h00; phy_valid = 1'b0; end
            ST_SYNC0: phy_data = 8'ha5;
            ST_SYNC1: phy_data = 8'h5a;
            ST_VERSION: phy_data = 8'h01;
            ST_TYPE: phy_data = active_type;
            ST_SEQ0: phy_data = active_sequence[7:0];
            ST_SEQ1: phy_data = active_sequence[15:8];
            ST_LEN0: phy_data = active_length[7:0];
            ST_LEN1: phy_data = {5'b00000, active_length[10:8]};
            ST_TS0: phy_data = active_timestamp[7:0];
            ST_TS1: phy_data = active_timestamp[15:8];
            ST_TS2: phy_data = active_timestamp[23:16];
            ST_TS3: phy_data = active_timestamp[31:24];
            ST_PAYLOAD: begin phy_data = payload_data; phy_valid = payload_valid; end
            ST_CRC0: phy_data = crc_value[7:0];
            ST_CRC1: phy_data = crc_value[15:8];
            default: begin phy_data = 8'h00; phy_valid = 1'b0; end
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            descriptor_pending <= 1'b0;
            pending_type <= 8'h00;
            pending_timestamp <= 32'h0000_0000;
            pending_length <= 11'd0;
            active_type <= 8'h00;
            active_timestamp <= 32'h0000_0000;
            active_length <= 11'd0;
            active_sequence <= 16'h0000;
            payload_index <= 11'd0;
            crc_value <= 16'hffff;
            state_code <= ST_IDLE;
            next_sequence <= 16'h0000;
            frame_count <= 32'h0000_0000;
            byte_count <= 32'h0000_0000;
            stall_periods <= 32'h0000_0000;
            max_stall_periods <= 32'h0000_0000;
        end else begin
            if (frame_valid && frame_ready) begin
                descriptor_pending <= 1'b1;
                pending_type <= frame_type;
                pending_timestamp <= frame_timestamp;
                pending_length <= frame_length;
            end

            if (link_fall_tick && phy_valid && !phy_ready) begin
                if (stall_periods != 32'hffff_ffff)
                    stall_periods <= stall_periods + 1'b1;
                if ((stall_periods != 32'hffff_ffff) &&
                    (stall_periods + 1'b1 > max_stall_periods))
                    max_stall_periods <= stall_periods + 1'b1;
            end else if (link_fall_tick) begin
                stall_periods <= 32'h0000_0000;
            end

            if ((state_code == ST_IDLE) && descriptor_pending && link_fall_tick) begin
                descriptor_pending <= 1'b0;
                active_type <= pending_type;
                active_timestamp <= pending_timestamp;
                active_length <= pending_length;
                active_sequence <= next_sequence;
                payload_index <= 11'd0;
                crc_value <= 16'hffff;
                state_code <= ST_SYNC0;
            end else if (byte_take) begin
                if (byte_count != 32'hffff_ffff)
                    byte_count <= byte_count + 1'b1;
                case (state_code)
                    ST_SYNC0: state_code <= ST_SYNC1;
                    ST_SYNC1: state_code <= ST_VERSION;
                    ST_VERSION: begin crc_value <= crc_next; state_code <= ST_TYPE; end
                    ST_TYPE: begin crc_value <= crc_next; state_code <= ST_SEQ0; end
                    ST_SEQ0: begin crc_value <= crc_next; state_code <= ST_SEQ1; end
                    ST_SEQ1: begin crc_value <= crc_next; state_code <= ST_LEN0; end
                    ST_LEN0: begin crc_value <= crc_next; state_code <= ST_LEN1; end
                    ST_LEN1: begin crc_value <= crc_next; state_code <= ST_TS0; end
                    ST_TS0: begin crc_value <= crc_next; state_code <= ST_TS1; end
                    ST_TS1: begin crc_value <= crc_next; state_code <= ST_TS2; end
                    ST_TS2: begin crc_value <= crc_next; state_code <= ST_TS3; end
                    ST_TS3: begin
                        crc_value <= crc_next;
                        if (active_length == 0)
                            state_code <= ST_CRC0;
                        else
                            state_code <= ST_PAYLOAD;
                    end
                    ST_PAYLOAD: begin
                        crc_value <= crc_next;
                        if (payload_index == active_length - 1'b1)
                            state_code <= ST_CRC0;
                        else
                            payload_index <= payload_index + 1'b1;
                    end
                    ST_CRC0: state_code <= ST_CRC1;
                    ST_CRC1: begin
                        state_code <= ST_IDLE;
                        next_sequence <= next_sequence + 1'b1;
                        if (frame_count != 32'hffff_ffff)
                            frame_count <= frame_count + 1'b1;
                    end
                    default: state_code <= ST_IDLE;
                endcase
            end
        end
    end
endmodule

`default_nettype wire

`timescale 1ns/1ps

module ingress_uart_rx #(
    parameter integer CLKS_PER_BIT = 26
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       uart_rx,
    output reg  [7:0] byte_data,
    output reg        byte_valid,
    output reg        framing_error,
    output reg        busy
);

    localparam [1:0] ST_IDLE  = 2'd0;
    localparam [1:0] ST_START = 2'd1;
    localparam [1:0] ST_DATA  = 2'd2;
    localparam [1:0] ST_STOP  = 2'd3;
    localparam integer HALF_BIT = CLKS_PER_BIT / 2;

    reg       rx_meta;
    reg       rx_sync;
    reg [1:0] state;
    reg [15:0] clock_count;
    reg [2:0] bit_index;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_meta <= 1'b1;
            rx_sync <= 1'b1;
        end else begin
            rx_meta <= uart_rx;
            rx_sync <= rx_meta;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            byte_data     <= 8'd0;
            byte_valid    <= 1'b0;
            framing_error <= 1'b0;
            busy          <= 1'b0;
            state         <= ST_IDLE;
            clock_count   <= 16'd0;
            bit_index     <= 3'd0;
        end else begin
            byte_valid    <= 1'b0;
            framing_error <= 1'b0;

            case (state)
                ST_IDLE: begin
                    busy        <= 1'b0;
                    clock_count <= 16'd0;
                    bit_index   <= 3'd0;
                    if (!rx_sync) begin
                        state <= ST_START;
                        busy  <= 1'b1;
                    end
                end

                ST_START: begin
                    if (clock_count == HALF_BIT - 1) begin
                        clock_count <= 16'd0;
                        if (!rx_sync) begin
                            state <= ST_DATA;
                        end else begin
                            state         <= ST_IDLE;
                            busy          <= 1'b0;
                            framing_error <= 1'b1;
                        end
                    end else begin
                        clock_count <= clock_count + 1'b1;
                    end
                end

                ST_DATA: begin
                    if (clock_count == CLKS_PER_BIT - 1) begin
                        clock_count          <= 16'd0;
                        byte_data[bit_index] <= rx_sync;
                        if (bit_index == 3'd7) begin
                            bit_index <= 3'd0;
                            state     <= ST_STOP;
                        end else begin
                            bit_index <= bit_index + 1'b1;
                        end
                    end else begin
                        clock_count <= clock_count + 1'b1;
                    end
                end

                ST_STOP: begin
                    if (clock_count == CLKS_PER_BIT - 1) begin
                        clock_count <= 16'd0;
                        state       <= ST_IDLE;
                        busy        <= 1'b0;
                        if (rx_sync)
                            byte_valid <= 1'b1;
                        else
                            framing_error <= 1'b1;
                    end else begin
                        clock_count <= clock_count + 1'b1;
                    end
                end

                default: begin
                    state <= ST_IDLE;
                    busy  <= 1'b0;
                end
            endcase
        end
    end

endmodule

`default_nettype none

module link_tx_wrapper #(
    parameter integer LINK_HALF_DIV = 12,
    parameter integer HEARTBEAT_CYCLES = 2400000,
    parameter integer SELFTEST_DATA_BYTES = 32,
    parameter [7:0]   SELFTEST_SEED = 8'h5a
) (
    input  wire        clk_24m,
    input  wire        rst_n,

    input  wire        frame_valid,
    output wire        frame_ready,
    input  wire [7:0]  frame_type,
    input  wire [31:0] frame_timestamp,
    input  wire [10:0] frame_length,
    input  wire        payload_valid,
    output wire        payload_ready,
    input  wire [7:0]  payload_data,
    input  wire [1:0]  test_mode,

    output wire [7:0]  link_data,
    output reg         link_clk,
    output wire        link_valid,
    input  wire        link_ready,

    output wire        busy,
    output wire [4:0]  state_code,
    output wire [15:0] next_sequence,
    output wire [31:0] tx_frame_count,
    output wire [31:0] tx_byte_count,
    output reg  [31:0] heartbeat_count,
    output reg  [31:0] selftest_frame_count,
    output reg  [31:0] error_count,
    output reg  [7:0]  last_error_code,
    output wire [31:0] max_stall_periods
);
    localparam integer DIV_WIDTH = (LINK_HALF_DIV <= 2) ? 1 : $clog2(LINK_HALF_DIV);
    localparam [10:0] SELFTEST_LENGTH = SELFTEST_DATA_BYTES + 2;

    reg [DIV_WIDTH-1:0] link_div_count;
    wire link_toggle;
    wire link_fall_tick;
    wire local_rst_n;
    reset_sync_n tx_reset_sync (.clk(clk_24m), .async_rst_n(rst_n), .sync_rst_n(local_rst_n));
    reg accepted_at_rise;
    reg payload_buffer_valid;
    reg [7:0] payload_buffer_data;
    wire source_payload_ready;

    reg [4:0] us_divider;
    reg [31:0] local_timestamp_us;
    reg [31:0] heartbeat_timer;
    wire heartbeat_due;

    wire core_frame_valid;
    wire core_frame_ready;
    wire [7:0] core_frame_type;
    wire [31:0] core_frame_timestamp;
    wire [10:0] core_frame_length;
    wire core_payload_valid;
    wire core_payload_ready;
    wire [7:0] core_payload_data;
    wire external_selected;
    wire heartbeat_selected;
    wire test_selected;

    reg external_payload_active;
    reg [10:0] external_payload_index;
    reg [10:0] external_payload_length;
    reg test_payload_active;
    reg [10:0] test_payload_index;
    reg [7:0] test_pattern;
    reg [1:0] test_mode_active;
    wire [7:0] test_prbs_next;
    wire [7:0] test_payload_data;

    assign link_toggle = (link_div_count == LINK_HALF_DIV-1);
    assign link_fall_tick = link_toggle && link_clk;
    assign heartbeat_due = (heartbeat_timer >= HEARTBEAT_CYCLES-1);
    assign external_selected = (test_mode == 0) && frame_valid &&
                               !heartbeat_due && (frame_length <= 1024);
    assign heartbeat_selected = (test_mode == 0) && heartbeat_due;
    assign test_selected = (test_mode != 0) && !test_payload_active;

    assign core_frame_valid = test_selected || external_selected || heartbeat_selected;
    assign core_frame_type = (test_mode != 0) ? 8'h7e :
                             (external_selected ? frame_type : 8'h11);
    assign core_frame_timestamp = (test_mode != 0) ? local_timestamp_us :
                                  (external_selected ? frame_timestamp :
                                                       local_timestamp_us);
    assign core_frame_length = (test_mode != 0) ? SELFTEST_LENGTH :
                               (external_selected ? frame_length : 11'd0);
    assign frame_ready = local_rst_n && (test_mode == 0) && core_frame_ready && !heartbeat_due;

    assign core_payload_valid = payload_buffer_valid;
    assign core_payload_data = payload_buffer_data;
    assign source_payload_ready = link_fall_tick &&
                                  (!payload_buffer_valid || core_payload_ready);
    assign payload_ready = external_payload_active && source_payload_ready;

    assign test_prbs_next = {test_pattern[6:0],
                             test_pattern[7] ^ test_pattern[5] ^
                             test_pattern[4] ^ test_pattern[3]};
    assign test_payload_data = (test_payload_index == 0) ?
                               ((test_mode_active == 1) ? 8'h00 : 8'h01) :
                               ((test_payload_index == 1) ? SELFTEST_SEED :
                                ((test_mode_active == 1) ? test_pattern + 1'b1 :
                                                   test_prbs_next));

    always @(posedge clk_24m or negedge local_rst_n) begin
        if (!local_rst_n) begin
            accepted_at_rise <= 1'b0;
            payload_buffer_valid <= 1'b0;
            payload_buffer_data <= 8'h00;
        end else begin
            if (link_toggle && !link_clk)
                accepted_at_rise <= link_valid && link_ready;
            if (link_fall_tick) begin
                if (source_payload_ready) begin
                    payload_buffer_valid <= external_payload_active ?
                                            payload_valid : test_payload_active;
                    if (external_payload_active && payload_valid)
                        payload_buffer_data <= payload_data;
                    else if (test_payload_active)
                        payload_buffer_data <= test_payload_data;
                end
            end
        end
    end

    always @(posedge clk_24m or negedge local_rst_n) begin
        if (!local_rst_n) begin
            link_div_count <= {DIV_WIDTH{1'b0}};
            link_clk <= 1'b0;
        end else if (link_toggle) begin
            link_div_count <= {DIV_WIDTH{1'b0}};
            link_clk <= ~link_clk;
        end else begin
            link_div_count <= link_div_count + 1'b1;
        end
    end

    always @(posedge clk_24m or negedge local_rst_n) begin
        if (!local_rst_n) begin
            us_divider <= 5'd0;
            local_timestamp_us <= 32'h0000_0000;
        end else if (us_divider == 5'd23) begin
            us_divider <= 5'd0;
            local_timestamp_us <= local_timestamp_us + 1'b1;
        end else begin
            us_divider <= us_divider + 1'b1;
        end
    end

    always @(posedge clk_24m or negedge local_rst_n) begin
        if (!local_rst_n) begin
            heartbeat_timer <= 32'h0000_0000;
            heartbeat_count <= 32'h0000_0000;
            selftest_frame_count <= 32'h0000_0000;
            error_count <= 32'd0;
            last_error_code <= 8'd0;
            external_payload_active <= 1'b0;
            external_payload_index <= 11'd0;
            external_payload_length <= 11'd0;
            test_payload_active <= 1'b0;
            test_payload_index <= 11'd0;
            test_pattern <= SELFTEST_SEED;
            test_mode_active <= 2'd0;
        end else begin
            if (test_mode != 0) begin
                heartbeat_timer <= 32'h0000_0000;
            end else if (heartbeat_selected && core_frame_ready) begin
                heartbeat_timer <= 32'h0000_0000;
            end else if (!heartbeat_due) begin
                heartbeat_timer <= heartbeat_timer + 1'b1;
            end

            if (frame_valid && frame_ready && frame_length > 1024) begin
                if (error_count != 32'hffff_ffff)
                    error_count <= error_count + 1'b1;
                last_error_code <= 8'h22;
            end

            if (core_frame_valid && core_frame_ready) begin
                if (test_selected) begin
                    test_payload_active <= 1'b1;
                    test_payload_index <= 11'd0;
                    test_pattern <= SELFTEST_SEED;
                    test_mode_active <= test_mode;
                    if (selftest_frame_count != 32'hffff_ffff)
                        selftest_frame_count <= selftest_frame_count + 1'b1;
                end else if (external_selected) begin
                    external_payload_active <= (frame_length != 0);
                    external_payload_index <= 11'd0;
                    external_payload_length <= frame_length;
                end else if (heartbeat_selected) begin
                    if (heartbeat_count != 32'hffff_ffff)
                        heartbeat_count <= heartbeat_count + 1'b1;
                end
            end

            if (external_payload_active && payload_valid && source_payload_ready) begin
                if (external_payload_index == external_payload_length - 1'b1) begin
                    external_payload_active <= 1'b0;
                    external_payload_index <= 11'd0;
                end else begin
                    external_payload_index <= external_payload_index + 1'b1;
                end
            end

            if (test_payload_active && source_payload_ready) begin
                if (test_payload_index >= 2)
                    test_pattern <= (test_mode_active == 1) ?
                                    test_pattern + 1'b1 : test_prbs_next;
                if (test_payload_index == SELFTEST_LENGTH-1) begin
                    test_payload_active <= 1'b0;
                    test_payload_index <= 11'd0;
                end else begin
                    test_payload_index <= test_payload_index + 1'b1;
                end
            end
        end
    end

    link_frame_tx frame_tx (
        .clk(clk_24m),
        .rst_n(local_rst_n),
        .link_fall_tick(link_fall_tick),
        .phy_ready(accepted_at_rise),
        .frame_valid(core_frame_valid),
        .frame_ready(core_frame_ready),
        .frame_type(core_frame_type),
        .frame_timestamp(core_frame_timestamp),
        .frame_length(core_frame_length),
        .payload_valid(core_payload_valid),
        .payload_ready(core_payload_ready),
        .payload_data(core_payload_data),
        .phy_data(link_data),
        .phy_valid(link_valid),
        .busy(busy),
        .state_code(state_code),
        .next_sequence(next_sequence),
        .frame_count(tx_frame_count),
        .byte_count(tx_byte_count),
        .max_stall_periods(max_stall_periods)
    );
endmodule

`default_nettype wire

`default_nettype none
// Generated by scripts/verify_hybrid_filterbank.py; Q14 feedback signs included.
module hybrid_filter_coefficients #(parameter integer FRONT=2) (
input wire [3:0] stage,input wire [2:0] term,output reg signed [15:0] coefficient);
wire [6:0] address={stage,2'b00}+stage+term;
always @* begin
coefficient=0;
case(FRONT)
0:case(address)
7'd0:coefficient=16'sd16131;
7'd1:coefficient=-16'sd16131;
7'd2:coefficient=16'sd0;
7'd3:coefficient=16'sd15877;
7'd4:coefficient=16'sd0;
7'd5:coefficient=16'sd16135;
7'd6:coefficient=-16'sd30690;
7'd7:coefficient=16'sd16135;
7'd8:coefficient=16'sd30690;
7'd9:coefficient=-16'sd15885;
7'd10:coefficient=16'sd3384;
7'd11:coefficient=16'sd6769;
7'd12:coefficient=16'sd3384;
7'd13:coefficient=16'sd6054;
7'd14:coefficient=-16'sd3208;
7'd15:coefficient=16'sd2993;
7'd16:coefficient=16'sd0;
7'd17:coefficient=-16'sd2993;
7'd18:coefficient=16'sd25820;
7'd19:coefficient=-16'sd10398;
7'd20:coefficient=16'sd4337;
7'd21:coefficient=16'sd0;
7'd22:coefficient=-16'sd4337;
7'd23:coefficient=16'sd15695;
7'd24:coefficient=-16'sd7710;
7'd25:coefficient=16'sd8192;
7'd26:coefficient=16'sd0;
7'd27:coefficient=-16'sd8192;
7'd28:coefficient=-16'sd10519;
7'd29:coefficient=16'sd0;
default:coefficient=0;
endcase
1:case(address)
7'd0:coefficient=16'sd14991;
7'd1:coefficient=-16'sd29982;
7'd2:coefficient=16'sd14991;
7'd3:coefficient=16'sd29863;
7'd4:coefficient=-16'sd13716;
7'd5:coefficient=16'sd16282;
7'd6:coefficient=-16'sd30970;
7'd7:coefficient=16'sd16282;
7'd8:coefficient=16'sd30970;
7'd9:coefficient=-16'sd16179;
7'd10:coefficient=16'sd13117;
7'd11:coefficient=16'sd26234;
7'd12:coefficient=16'sd13117;
7'd13:coefficient=-16'sd25576;
7'd14:coefficient=-16'sd10508;
7'd15:coefficient=16'sd2993;
7'd16:coefficient=16'sd0;
7'd17:coefficient=-16'sd2993;
7'd18:coefficient=16'sd25820;
7'd19:coefficient=-16'sd10398;
7'd20:coefficient=16'sd4337;
7'd21:coefficient=16'sd0;
7'd22:coefficient=-16'sd4337;
7'd23:coefficient=16'sd15695;
7'd24:coefficient=-16'sd7710;
7'd25:coefficient=16'sd8192;
7'd26:coefficient=16'sd0;
7'd27:coefficient=-16'sd8192;
7'd28:coefficient=-16'sd10519;
7'd29:coefficient=16'sd0;
default:coefficient=0;
endcase
2:case(address)
7'd0:coefficient=16'sd14991;
7'd1:coefficient=-16'sd29982;
7'd2:coefficient=16'sd14991;
7'd3:coefficient=16'sd29863;
7'd4:coefficient=-16'sd13716;
7'd5:coefficient=16'sd16172;
7'd6:coefficient=-16'sd30762;
7'd7:coefficient=16'sd16172;
7'd8:coefficient=16'sd30762;
7'd9:coefficient=-16'sd15961;
7'd10:coefficient=16'sd15966;
7'd11:coefficient=-16'sd25833;
7'd12:coefficient=16'sd15966;
7'd13:coefficient=16'sd25833;
7'd14:coefficient=-16'sd15548;
7'd15:coefficient=16'sd15765;
7'd16:coefficient=-16'sd18532;
7'd17:coefficient=16'sd15765;
7'd18:coefficient=16'sd18532;
7'd19:coefficient=-16'sd15145;
7'd20:coefficient=16'sd15568;
7'd21:coefficient=-16'sd9622;
7'd22:coefficient=16'sd15568;
7'd23:coefficient=16'sd9622;
7'd24:coefficient=-16'sd14752;
7'd25:coefficient=16'sd13117;
7'd26:coefficient=16'sd26234;
7'd27:coefficient=16'sd13117;
7'd28:coefficient=-16'sd25576;
7'd29:coefficient=-16'sd10508;
7'd30:coefficient=16'sd2993;
7'd31:coefficient=16'sd0;
7'd32:coefficient=-16'sd2993;
7'd33:coefficient=16'sd25820;
7'd34:coefficient=-16'sd10398;
7'd35:coefficient=16'sd4337;
7'd36:coefficient=16'sd0;
7'd37:coefficient=-16'sd4337;
7'd38:coefficient=16'sd15695;
7'd39:coefficient=-16'sd7710;
7'd40:coefficient=16'sd8192;
7'd41:coefficient=16'sd0;
7'd42:coefficient=-16'sd8192;
7'd43:coefficient=-16'sd10519;
7'd44:coefficient=16'sd0;
default:coefficient=0;
endcase
default:coefficient=0;
endcase
end
endmodule
`default_nettype wire

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

`timescale 1ns/1ps
`default_nettype none
// Matches EMG Studio V3.0: 8 little-endian float32 mV + 00 00 80 7F.
// Discards nonfinite/diagnostic frames. Applies the exact existing channel
// map [3,2,1,0,4,5,6,7] ONCE, then np.rint (ties-to-even) and clamp0..3300.
module justfloat_ingress #(
    parameter integer CLKS_PER_BIT=26,
    parameter integer GAP_CYCLES=6000000
) (
    input wire clk,input wire rst_n,input wire enable,input wire rx,input wire [31:0] timestamp_us,
    output wire sample_valid,input wire sample_ready,output wire [127:0] sample_data,
    output wire [15:0] sample_seq,output wire [31:0] sample_timestamp,
    output reg discontinuity,output reg [31:0] good_frames,output reg [31:0] bad_frames,
    output reg [31:0] diagnostic_frames,output reg [31:0] overflows
);
    wire [7:0] byte_data;wire byte_valid,framing,busy;
    ingress_uart_rx #(.CLKS_PER_BIT(CLKS_PER_BIT)) receiver(clk,rst_n,rx,byte_data,byte_valid,framing,busy);
    reg [287:0] shift;
    wire [287:0] shifted={byte_data,shift[287:8]};
    reg [5:0] byte_count;
    reg [255:0] payload;
    reg converting;reg [2:0] channel;reg [127:0] converted;
    reg [31:0] frame_timestamp;
    wire [2:0] source_channel=channel<4 ? 3'd3-channel : channel;
    wire [31:0] value=payload[source_channel*32 +:32];
    reg finite;
    integer f;
    always @* begin
        finite=1;
        for(f=0;f<8;f=f+1) if(shifted[f*32+23 +:8]==8'hff) finite=0;
    end
    wire diagnostic=shifted[31:0]>=32'hc7f11fc1 && shifted[31:0]<=32'hc7f1203f;
 function [15:0] millivolts;
  input [31:0] x;reg [23:0] sig;reg [11:0] whole;reg increment;reg [12:0] rounded;
  begin
   sig={1'b1,x[22:0]};whole=0;increment=0;rounded=0;
   if(x[31] || x[30:23]<126) millivolts=0;
   else if(x[30:23]>=139) millivolts=3300;
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
    millivolts=rounded>3300 ? 16'd3300 : {3'b0,rounded};
   end
  end
 endfunction
    reg [175:0] fifo[0:7];
    reg [2:0] read_pointer,write_pointer;reg [3:0] level;
    reg [15:0] sample_sequence;
    reg [23:0] gap_timer;reg have_sample;
    wire pop=sample_valid && sample_ready;
    wire push=converting && channel==7 && enable;
    wire full=level==8 && !pop;
    assign sample_valid=level!=0 && enable;
    assign {sample_timestamp,sample_seq,sample_data}=fifo[read_pointer];
    always @(posedge clk or negedge rst_n) begin
        if(!rst_n) begin
            shift<=0;byte_count<=0;payload<=0;converting<=0;channel<=0;converted<=0;frame_timestamp<=0;
            read_pointer<=0;write_pointer<=0;level<=0;sample_sequence<=0;gap_timer<=0;have_sample<=0;
            discontinuity<=0;good_frames<=0;bad_frames<=0;diagnostic_frames<=0;overflows<=0;
        end else begin
            discontinuity<=0;
            if(enable && gap_timer<GAP_CYCLES) gap_timer<=gap_timer+1'b1;
            if(!enable) begin
                shift<=0;byte_count<=0;converting<=0;read_pointer<=0;write_pointer<=0;level<=0;gap_timer<=0;have_sample<=0;
            end else if(framing) begin
                shift<=0;byte_count<=0;converting<=0;read_pointer<=0;write_pointer<=0;level<=0;
                bad_frames<=bad_frames+1'b1;discontinuity<=1;
            end else begin
                case({push && !full,pop})
                    2'b10:level<=level+1'b1;
                    2'b01:level<=level-1'b1;
                endcase
                if(pop) read_pointer<=read_pointer+1'b1;
                if(converting) begin
                    converted[channel*16 +:16]<=millivolts(value);
                    if(channel==7) begin
                        converting<=0;
                        if(full) begin
                            overflows<=overflows+1'b1;discontinuity<=1;
                            read_pointer<=0;write_pointer<=0;level<=0;
                        end else begin
                            fifo[write_pointer]<={frame_timestamp,sample_sequence,millivolts(value),converted[111:0]};
                            write_pointer<=write_pointer+1'b1;sample_sequence<=sample_sequence+1'b1;good_frames<=good_frames+1'b1;
                        end
                    end else channel<=channel+1'b1;
                end
                if(byte_valid) begin
                    shift<=shifted;if(byte_count<36) byte_count<=byte_count+1'b1;
                    if(shifted[287:256]==32'h7f800000) begin
                        byte_count<=0;
                        if(byte_count<35 || !finite || converting) begin bad_frames<=bad_frames+1'b1;end
                        else if(diagnostic) diagnostic_frames<=diagnostic_frames+1'b1;
                        else begin
                            payload<=shifted[255:0];converting<=1;channel<=0;frame_timestamp<=timestamp_us;
                            gap_timer<=0;have_sample<=1;
                            if(have_sample && gap_timer>=GAP_CYCLES) begin
                                discontinuity<=1;read_pointer<=0;write_pointer<=0;level<=0;
                            end
                        end
                    end
                end
            end
        end
    end
endmodule
`default_nettype wire

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

`default_nettype none
// Independent result and whole-sample mailboxes. Congestion never stalls DSP.
// Two byte-RAM slots replace the 832-bit pending and active shift registers.
// Producer contract: wave_data[831:128] stays stable for 104 clocks after
// wave_valid. fusion_features takes >7000 clocks per sample; only its header
// can change immediately, so the header is captured on the valid edge.
module v2_packet_mux (
 input wire clk,input wire rst_n,input wire flush,input wire [31:0] timestamp,
 input wire wave_valid,input wire [831:0] wave_data,
 input wire result_valid,input wire [255:0] result_data,
 output reg [31:0] wave_drops,output reg [31:0] result_drops,
 output wire frame_valid,input wire frame_ready,output reg [7:0] frame_type,
 output reg [10:0] frame_length,output reg [31:0] frame_timestamp,
 output wire payload_valid,input wire payload_ready,output wire [7:0] payload_data,
 input wire management_valid,output wire management_ready,input wire management_stop,
 input wire [10:0] management_length,input wire management_payload_valid,
 output wire management_payload_ready,input wire [7:0] management_data
);
 (* ram_style="bram" *) reg [7:0] packets[0:255];
 reg [7:0] packet_q;
 reg [127:0] capture_header;
 reg [255:0] result_hold;
 (* ram_style="bram" *) reg [7:0] result_packets[0:63];
 reg [7:0] result_q;reg result_capturing,result_capture_bank,result_pending_bank,result_active_bank;
 reg [4:0] result_capture_index,result_read_index;reg [1:0] result_slots;
 wire [4:0] result_prefetch=(state==2 && frame_type==8'h41 && payload_ready && left>1) ? result_read_index+1'b1 : result_read_index;
 always @(posedge clk) begin
  result_q<=result_packets[{result_active_bank,result_prefetch}];
  if(result_capturing && !flush) result_packets[{result_capture_bank,result_capture_index}]<=result_hold[result_capture_index*8 +:8];
 end
 reg [1:0] slots_busy;
 reg capturing,capture_bank,pending_bank,active_bank;
 reg [6:0] capture_index,read_index;
 reg [31:0] capture_time;
 reg [31:0] wave_time,result_time;
 reg wave_pending,result_pending;
 reg [1:0] state;
 reg [6:0] left;reg last_management;
 assign frame_valid=state==1;
 assign payload_valid=state==2 && (frame_type!=8'h42 || management_payload_valid);
 wire choose_management=management_valid && (management_stop || (!result_pending && (!wave_pending || !last_management))) && !flush;
 assign management_ready=state==0 && choose_management;
 assign management_payload_ready=state==2 && frame_type==8'h42 && payload_ready;
 wire [6:0] prefetch_index=(state==2 && payload_ready && left>1) ? read_index+1'b1 : read_index;
 wire [7:0] capture_byte=capture_index<16 ? capture_header[capture_index*8 +:8] : wave_data[capture_index*8 +:8];
 wire packet_write=!flush && (capturing || (wave_valid && slots_busy!=2'b11));
 wire [7:0] packet_write_address=capturing ? {capture_bank,capture_index} : {slots_busy[0],7'b0};
 wire [7:0] packet_write_data=capturing ? capture_byte : wave_data[7:0];
 assign payload_data=frame_type==8'h42 ? management_data : (frame_type==8'h40 ? packet_q : result_q);
 always @(posedge clk) begin
  packet_q<=packets[{active_bank,prefetch_index}];
  if(packet_write) packets[packet_write_address]<=packet_write_data;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   result_slots<=0;result_capturing<=0;result_capture_bank<=0;result_pending_bank<=0;result_active_bank<=0;result_capture_index<=0;result_read_index<=0;
   wave_pending<=0;result_pending<=0;state<=0;left<=0;last_management<=0;
   slots_busy<=0;capturing<=0;capture_bank<=0;pending_bank<=0;active_bank<=0;
   capture_index<=0;read_index<=0;capture_time<=0;
   frame_type<=0;frame_length<=0;frame_timestamp<=0;wave_drops<=0;result_drops<=0;
  end else begin
   if(flush) begin
    wave_pending<=0;result_pending<=0;capturing<=0;result_capturing<=0;
    result_slots<=(state!=0 && frame_type==8'h41) ? (result_active_bank ? 2'b10 : 2'b01) : 2'b00;
    slots_busy<=(state!=0 && frame_type==8'h40) ? (active_bank ? 2'b10 : 2'b01) : 2'b00;
   end
   if(capturing && !flush) begin
    if(capture_index==103) begin
     capturing<=0;wave_pending<=1;pending_bank<=capture_bank;wave_time<=capture_time;
    end else capture_index<=capture_index+1'b1;
   end
   if(wave_valid && !flush) begin
    if(capturing || wave_pending || slots_busy==2'b11) wave_drops<=wave_drops+1'b1;
    else begin
     capture_bank<=slots_busy[0];slots_busy[slots_busy[0]]<=1;
     capture_header<=wave_data[127:0];capture_time<=timestamp;
     capture_index<=1;capturing<=1;
    end
   end
   if(result_capturing && !flush) begin
    result_capture_index<=result_capture_index+1'b1;
    if(result_capture_index==31) begin result_capturing<=0;result_pending<=1;result_pending_bank<=result_capture_bank;end
   end
   if(result_valid) begin
    if((result_pending || result_capturing || result_slots==2'b11) && !flush) result_drops<=result_drops+1'b1;
    else begin
     result_hold<=result_data;result_time<=timestamp;result_capturing<=1;result_capture_index<=0;
     result_capture_bank<=flush ? (state!=0 && frame_type==8'h41 && !result_active_bank) : result_slots[0];
     if(flush) result_slots[(state!=0 && frame_type==8'h41 && !result_active_bank)]<=1;
     else result_slots[result_slots[0]]<=1;
    end
   end
   case(state)
    0:if(choose_management) begin
     last_management<=1;frame_type<=8'h42;frame_length<=management_length;left<=management_length[6:0];frame_timestamp<=timestamp;state<=1;
    end else if(result_pending && !flush) begin
     last_management<=0;result_active_bank<=result_pending_bank;result_read_index<=0;frame_type<=8'h41;frame_length<=32;
     left<=32;frame_timestamp<=result_time;result_pending<=0;state<=1;
    end else if(wave_pending && !flush) begin
     last_management<=0;active_bank<=pending_bank;read_index<=0;frame_type<=8'h40;frame_length<=104;
     left<=104;frame_timestamp<=wave_time;wave_pending<=0;state<=1;
    end
    1:if(frame_ready) state<=2;
    2:if(payload_ready && payload_valid) begin
     if(frame_type==8'h40) begin
      read_index<=read_index+1'b1;
      if(left==1) slots_busy[active_bank]<=0;
     end else if(frame_type==8'h41) begin result_read_index<=result_read_index+1'b1;if(left==1) result_slots[result_active_bank]<=0;end
     left<=left-1'b1;if(left==1) state<=0;
    end
   endcase
  end
 end
endmodule
`default_nettype wire

`default_nettype none
// Bounded binary commands. Canonical metadata is validated byte-for-byte.
// In-place transaction: BEGIN disables inference, any failure needs full reload.
module v2_pc #(parameter integer CAPACITY=32,parameter integer BIT_CYCLES=26,
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

`default_nettype none
module v2_board_top #(parameter integer CAPACITY=32,parameter integer UART_BIT_CYCLES=26)(
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

`default_nettype none
// One ordinary packet + a separate STOP slot. No command is retried.
// The slot remains occupied until the complete payload has been consumed.
module gt_forward(
 input wire clk,rst_n,flush,
 input wire start,input wire is_stop,input wire [6:0] length,
 output wire ready,input wire valid,output wire take,input wire [7:0] data,
 output wire frame_valid,input wire frame_ready,output wire frame_stop,
 output wire [10:0] frame_length,output wire payload_valid,
 input wire payload_ready,output wire [7:0] payload_data,
 output reg [15:0] cancelled,output wire [3:0] queue_state
);
 (* ram_style="bram" *) reg [7:0] memory[0:63];
 reg [7:0] q;reg [5:0] wr,rd;
 reg writing,write_stop,pending,stop_pending,active,active_stop;
 reg [6:0] size,left,write_left;reg [63:0] stop_header;
 assign ready=!writing && (is_stop ? !stop_pending && !(active && active_stop) : !pending && !(active && !active_stop));
 assign take=writing;
 assign frame_stop=stop_pending;
 assign frame_valid=!active && (pending || stop_pending) && !flush && !writing;
 assign frame_length=stop_pending ? 11'd8 : {4'b0,size};
 assign payload_valid=active;
 assign payload_data=active_stop ? stop_header[7:0] : q;
 assign queue_state={active,writing,stop_pending,pending};
 wire [5:0] prefetch=(active && !active_stop && payload_ready && left>1) ? rd+1'b1 : rd;
 always @(posedge clk) begin
  q<=memory[prefetch];
  if(writing && valid && !write_stop) memory[wr]<=data;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   wr<=0;rd<=0;writing<=0;write_stop<=0;pending<=0;stop_pending<=0;
   active<=0;active_stop<=0;size<=0;left<=0;write_left<=0;stop_header<=0;cancelled<=0;
  end else begin
   if(flush) begin
    pending<=0;stop_pending<=0;
    if(pending || stop_pending || writing) cancelled<=cancelled+1'b1;
    // An already started frame always finishes. PC enqueue cannot overlap
    // local MODEL_BEGIN; a stream discontinuity during a copy cancels it.
    writing<=0;
   end else begin
    if(start && ready) begin writing<=1;write_stop<=is_stop;wr<=0;write_left<=length;if(!is_stop) size<=length;end
    if(writing && valid) begin
     if(write_stop) stop_header[wr*8 +:8]<=data;
     wr<=wr+1'b1;write_left<=write_left-1'b1;
     if(write_left==1) begin writing<=0;if(write_stop) stop_pending<=1;else pending<=1;end
    end
    if(frame_valid && frame_ready) begin
     active<=1;active_stop<=stop_pending;rd<=0;
     left<=stop_pending ? 7'd8 : size;
     if(stop_pending) stop_pending<=0;else pending<=0;
    end
   end
   if(active && payload_ready) begin
    if(active_stop) stop_header<=stop_header>>8;else rd<=rd+1'b1;
    left<=left-1'b1;if(left==1) active<=0;
   end
  end
 end
endmodule
`default_nettype wire

`default_nettype none
// Two complete CRC-checked packet banks. A full bank is never overwritten.
// UART cannot be backpressured; full/invalid/timeout frames are counted once.
module gt_return #(parameter integer BIT_CYCLES=26)(
 input wire clk,rst_n,rx,
 output wire frame_valid,input wire frame_ready,output wire [7:0] frame_type,
 output wire [10:0] frame_length,output wire payload_valid,input wire payload_ready,
 output wire [7:0] payload_data,output wire [31:0] errors,
 output wire event_valid,output wire [15:0] event_id,event_opcode,output wire event_final
);
 wire [7:0] rb;wire bv,fe;
 ingress_uart_rx #(.CLKS_PER_BIT(BIT_CYCLES)) uart(clk,rst_n,rx,rb,bv,fe,/*busy*/);
 (* ram_style="bram" *) reg [7:0] memory[0:2047];
 reg [7:0] q;reg [3:0] state;
 reg [10:0] length,index,out_index,out_left;
 reg [7:0] kind,crc_low;
 reg [79:0] header;
 reg [15:0] crc,error_count;
 reg [17:0] idle_count;
 reg write_bank,read_bank,discard,active;
 reg [1:0] occupied;
 reg [7:0] types[0:1];reg [10:0] lengths[0:1];
 wire [15:0] crc_next;
 link_crc16_ccitt step(crc,rb,crc_next);
 wire header_ok=(kind==8'h51 || kind==8'h52) && length>=10 && header[7:0]==1 && header[15:8]==2 && header[79:64]==length-10;
 wire commit=bv && state==15 && {rb,crc_low}==crc && header_ok && !discard;
 assign event_valid=commit;assign event_id=header[31:16];assign event_opcode=header[47:32];
 assign event_final=kind==8'h52 || header[63:48]!=1;
 assign errors={16'b0,error_count};
 assign frame_valid=occupied[read_bank] && !active;
 assign frame_type=types[read_bank];assign frame_length=lengths[read_bank];
 assign payload_valid=active;assign payload_data=q;
 wire [9:0] prefetch=(active && payload_ready && out_left>1) ? out_index[9:0]+1'b1 : out_index[9:0];
 always @(posedge clk) begin
  q<=memory[{read_bank,prefetch}];
  if(bv && state==13 && !discard) memory[{write_bank,index[9:0]}]<=rb;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   state<=0;length<=0;index<=0;kind<=0;crc_low<=0;header<=0;crc<=16'hffff;
   error_count<=0;idle_count<=0;write_bank<=0;read_bank<=0;discard<=0;active<=0;
   occupied<=0;out_index<=0;out_left<=0;
  end else begin
   if(frame_valid && frame_ready) begin active<=1;out_index<=0;out_left<=lengths[read_bank];end
   if(active && payload_ready) begin
    out_index<=out_index+1'b1;out_left<=out_left-1'b1;
    if(out_left==1) begin occupied[read_bank]<=0;read_bank<=!read_bank;active<=0;out_index<=0;end
   end
   if(fe || (state!=0 && !bv && idle_count==239999)) begin
    state<=0;idle_count<=0;if(error_count!=65535) error_count<=error_count+1'b1;
   end else if(bv) begin
    idle_count<=0;
    if(state>=2 && state<=13) crc<=crc_next;
    case(state)
     0:if(rb==8'ha5) state<=1;
     1:if(rb==8'h5a) begin state<=2;crc<=16'hffff;discard<=occupied[write_bank];header<=0;end
       else if(rb!=8'ha5) state<=0;
     2:if(rb==1) state<=3;else begin state<=0;if(error_count!=65535) error_count<=error_count+1'b1;end
     3:begin kind<=rb;state<=4;end
     4,5:state<=state+1'b1;
     6:begin length[7:0]<=rb;state<=7;end
     7:if({rb,length[7:0]}>1024) begin state<=0;if(error_count!=65535) error_count<=error_count+1'b1;end
       else begin length[10:8]<=rb[2:0];state<=8;end
     8,9,10:state<=state+1'b1;
     11:begin index<=0;state<=length==0 ? 14 : 13;end
     13:begin
      if(index<10) header[index*8 +:8]<=rb;
      index<=index+1'b1;if(index==length-1) state<=14;
     end
     14:begin crc_low<=rb;state<=15;end
     15:begin
      state<=0;
      if(commit) begin occupied[write_bank]<=1;types[write_bank]<=kind;lengths[write_bank]<=length;write_bank<=!write_bank;end
      else if(error_count!=65535) error_count<=error_count+1'b1;
     end
     default:state<=0;
    endcase
   end else if(state!=0) idle_count<=idle_count+1'b1;
  end
 end
endmodule
`default_nettype wire

`default_nettype none
module gt_usb_stream(
 input wire clk,rst_n,flush,
 input wire raw_valid,input wire [223:0] raw_data,
 input wire result_valid,input wire [255:0] result_data,
 output wire frame_valid,input wire frame_ready,output wire [7:0] frame_type,
 output wire [10:0] frame_length,output wire payload_valid,input wire payload_ready,
 output wire [7:0] payload_data,output reg [15:0] drops,output wire [4:0] queued
);
 (* ram_style="bram" *) reg [7:0] memory[0:511];
 reg [15:0] result_slot;
 reg [3:0] write_slot,read_slot;
 reg [4:0] count,index,copy_index;
 reg active,copying,copy_result;
 reg [255:0] capture;
 reg [7:0] q;
 wire finish=active && payload_ready && index==(result_slot[read_slot] ? 31 : 27);
 wire publish=copying && copy_index==(copy_result ? 31 : 27) && !flush;
 assign frame_valid=count!=0 && !active && !flush;
 assign frame_type=result_slot[read_slot] ? 8'h41 : 8'h43;
 assign frame_length=result_slot[read_slot] ? 11'd32 : 11'd28;
 assign payload_valid=active;
 assign payload_data=q;assign queued=count;
 wire [4:0] prefetch=active && payload_ready && !finish ? index+1'b1 : index;
 always @(posedge clk) begin
  q<=memory[{read_slot,prefetch}];
  if(copying && !flush) memory[{write_slot,copy_index}]<=capture[7:0];
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   result_slot<=0;write_slot<=0;read_slot<=0;count<=0;index<=0;copy_index<=0;
   active<=0;copying<=0;copy_result<=0;capture<=0;drops<=0;
  end else begin
   if(flush) begin
    copying<=0;count<=active && !finish ? 1 : 0;
    write_slot<=active && !finish ? read_slot+1'b1 : (finish ? read_slot+1'b1 : read_slot);
   end else begin
    case({publish,finish})
     2'b10:count<=count+1'b1;
     2'b01:count<=count-1'b1;
    endcase
   end
   if(frame_valid && frame_ready) begin active<=1;index<=0;end
   if(active && payload_ready) begin
    index<=index+1'b1;
    if(finish) begin active<=0;index<=0;read_slot<=read_slot+1'b1;end
   end
   if(copying && !flush) begin
    capture<=capture>>8;copy_index<=copy_index+1'b1;
    if(publish) begin copying<=0;result_slot[write_slot]<=copy_result;write_slot<=write_slot+1'b1;end
   end
   if(result_valid || (raw_valid && !flush)) begin
    // Results win an exact-cycle collision. Count the omitted raw group too,
    // so a sequence gap can never be reported as a lossless capture.
    if((copying || count==16) && !flush) begin
     if(raw_valid && result_valid) drops<=drops>=65534 ? 16'hffff : drops+2'd2;
     else if(drops!=65535)drops<=drops+1'b1;
    end
    else begin
     if(raw_valid && result_valid && !flush && drops!=65535)drops<=drops+1'b1;
     capture<=result_valid ? result_data : {32'b0,raw_data};
     copy_result<=result_valid;copying<=1;copy_index<=0;
    end
   end
  end
 end
endmodule
`default_nettype wire
