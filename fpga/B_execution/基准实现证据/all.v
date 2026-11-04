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

module link_async_fifo #(
    parameter integer DATA_WIDTH = 8,
    parameter integer ADDR_WIDTH = 4
) (
    input  wire                  wr_clk,
    input  wire                  wr_rst_n,
    input  wire                  wr_en,
    input  wire [DATA_WIDTH-1:0] wr_data,
    output reg                   wr_full,
    output wire [ADDR_WIDTH:0]   wr_level,

    input  wire                  rd_clk,
    input  wire                  rd_rst_n,
    input  wire                  rd_en,
    output wire [DATA_WIDTH-1:0] rd_data,
    output reg                   rd_empty,
    output wire [ADDR_WIDTH:0]   rd_level
);
    localparam integer DEPTH = (1 << ADDR_WIDTH);

    reg [DATA_WIDTH-1:0] memory [0:DEPTH-1];
    reg [ADDR_WIDTH:0] wr_binary;
    reg [ADDR_WIDTH:0] wr_gray;
    reg [ADDR_WIDTH:0] rd_binary;
    reg [ADDR_WIDTH:0] rd_gray;
    (* async_reg = "true" *) reg [ADDR_WIDTH:0] rd_gray_wr_sync1;
    (* async_reg = "true" *) reg [ADDR_WIDTH:0] rd_gray_wr_sync2;
    (* async_reg = "true" *) reg [ADDR_WIDTH:0] wr_gray_rd_sync1;
    (* async_reg = "true" *) reg [ADDR_WIDTH:0] wr_gray_rd_sync2;

    wire wr_take;
    wire rd_take;
    wire [ADDR_WIDTH:0] wr_binary_next;
    wire [ADDR_WIDTH:0] wr_gray_next;
    wire [ADDR_WIDTH:0] rd_binary_next;
    wire [ADDR_WIDTH:0] rd_gray_next;
    wire wr_full_next;
    wire rd_empty_next;
    wire [ADDR_WIDTH:0] rd_binary_sync_wr;
    wire [ADDR_WIDTH:0] wr_binary_sync_rd;

    function [ADDR_WIDTH:0] gray_to_binary;
        input [ADDR_WIDTH:0] gray_value;
        integer i;
        begin
            gray_to_binary[ADDR_WIDTH] = gray_value[ADDR_WIDTH];
            for (i = ADDR_WIDTH-1; i >= 0; i = i - 1)
                gray_to_binary[i] = gray_to_binary[i+1] ^ gray_value[i];
        end
    endfunction

    assign wr_take = wr_en && !wr_full;
    assign rd_take = rd_en && !rd_empty;
    assign wr_binary_next = wr_binary + wr_take;
    assign rd_binary_next = rd_binary + rd_take;
    assign wr_gray_next = (wr_binary_next >> 1) ^ wr_binary_next;
    assign rd_gray_next = (rd_binary_next >> 1) ^ rd_binary_next;
    assign wr_full_next = (wr_gray_next ==
                           {~rd_gray_wr_sync2[ADDR_WIDTH:ADDR_WIDTH-1],
                            rd_gray_wr_sync2[ADDR_WIDTH-2:0]});
    assign rd_empty_next = (rd_gray_next == wr_gray_rd_sync2);

    assign rd_data = memory[rd_binary[ADDR_WIDTH-1:0]];
    assign rd_binary_sync_wr = gray_to_binary(rd_gray_wr_sync2);
    assign wr_binary_sync_rd = gray_to_binary(wr_gray_rd_sync2);
    assign wr_level = wr_binary - rd_binary_sync_wr;
    assign rd_level = wr_binary_sync_rd - rd_binary;

    always @(posedge wr_clk or negedge wr_rst_n) begin
        if (!wr_rst_n) begin
            wr_binary <= {(ADDR_WIDTH+1){1'b0}};
            wr_gray <= {(ADDR_WIDTH+1){1'b0}};
            wr_full <= 1'b0;
        end else begin
            if (wr_take)
                memory[wr_binary[ADDR_WIDTH-1:0]] <= wr_data;
            wr_binary <= wr_binary_next;
            wr_gray <= wr_gray_next;
            wr_full <= wr_full_next;
        end
    end

    always @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) begin
            rd_binary <= {(ADDR_WIDTH+1){1'b0}};
            rd_gray <= {(ADDR_WIDTH+1){1'b0}};
            rd_empty <= 1'b1;
        end else begin
            rd_binary <= rd_binary_next;
            rd_gray <= rd_gray_next;
            rd_empty <= rd_empty_next;
        end
    end

    always @(posedge wr_clk or negedge wr_rst_n) begin
        if (!wr_rst_n) begin
            rd_gray_wr_sync1 <= {(ADDR_WIDTH+1){1'b0}};
            rd_gray_wr_sync2 <= {(ADDR_WIDTH+1){1'b0}};
        end else begin
            rd_gray_wr_sync1 <= rd_gray;
            rd_gray_wr_sync2 <= rd_gray_wr_sync1;
        end
    end

    always @(posedge rd_clk or negedge rd_rst_n) begin
        if (!rd_rst_n) begin
            wr_gray_rd_sync1 <= {(ADDR_WIDTH+1){1'b0}};
            wr_gray_rd_sync2 <= {(ADDR_WIDTH+1){1'b0}};
        end else begin
            wr_gray_rd_sync1 <= wr_gray;
            wr_gray_rd_sync2 <= wr_gray_rd_sync1;
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

module link_rx_wrapper #(
    parameter integer FIFO_ADDR_WIDTH = 4,
    parameter integer FRAME_TIMEOUT_CYCLES = 240000,
    parameter integer HEARTBEAT_TIMEOUT_CYCLES = 7200000
) (
    input  wire        clk_24m,
    input  wire        rst_n,
    input  wire [7:0]  link_data,
    input  wire        link_clk,
    input  wire        link_valid,
    output wire        link_ready,

    output wire        frame_valid,
    input  wire        frame_ready,
    output wire [7:0]  frame_type,
    output wire [15:0] frame_seq,
    output wire [31:0] frame_timestamp,
    output wire [10:0] frame_length,
    output wire        payload_valid,
    input  wire        payload_ready,
    output wire [7:0]  payload_data,
    output wire        payload_last,

    output wire        busy,
    output wire [4:0]  state_code,
    output wire [31:0] good_frame_count,
    output wire [31:0] rx_byte_count,
    output wire [31:0] crc_error_count,
    output wire [31:0] sequence_gap_count,
    output wire [31:0] length_error_count,
    output wire [31:0] version_error_count,
    output reg  [31:0] fifo_overflow_count,
    output wire [31:0] timeout_count,
    output wire [31:0] selftest_error_count,
    output wire [7:0]  last_error_code,
    output reg  [FIFO_ADDR_WIDTH:0] max_fifo_level,
    output reg  [31:0] last_frame_cycles,
    output reg  [31:0] heartbeat_age_cycles,
    output wire        heartbeat_timeout
);
    wire link_rst_n;
    wire local_rst_n;
    reset_sync_n rx_reset_sync (.clk(clk_24m), .async_rst_n(rst_n), .sync_rst_n(local_rst_n));
    wire fifo_full;
    wire fifo_empty;
    wire [7:0] fifo_data;
    wire fifo_read;
    wire parser_byte_ready;
    wire [FIFO_ADDR_WIDTH:0] fifo_wr_level;
    wire [FIFO_ADDR_WIDTH:0] fifo_rd_level;
    reg overflow_toggle;
    reg stalled_source_active;
    reg stalled_source_reported;
    reg [7:0] stalled_source_data;
    (* async_reg = "true" *) reg overflow_sync1;
    (* async_reg = "true" *) reg overflow_sync2;
    reg overflow_sync_last;
    wire overflow_event_24m;
    wire good_frame_pulse;
    wire [7:0] good_frame_type;
    reg [31:0] candidate_cycles;

    reset_sync_n link_reset_sync (
        .clk(link_clk),
        .async_rst_n(rst_n),
        .sync_rst_n(link_rst_n)
    );

    assign link_ready = link_rst_n && !fifo_full;
    assign fifo_read = !fifo_empty && parser_byte_ready;
    assign overflow_event_24m = overflow_sync2 ^ overflow_sync_last;
    assign heartbeat_timeout =
        (heartbeat_age_cycles >= HEARTBEAT_TIMEOUT_CYCLES);

    always @(posedge link_clk or negedge link_rst_n) begin
        if (!link_rst_n) begin
            overflow_toggle <= 1'b0;
            stalled_source_active <= 1'b0;
            stalled_source_reported <= 1'b0;
            stalled_source_data <= 8'h00;
        end else if (!link_ready) begin
            if (!stalled_source_active && link_valid) begin
                stalled_source_active <= 1'b1;
                stalled_source_reported <= 1'b0;
                stalled_source_data <= link_data;
            end else if (stalled_source_active &&
                         (!link_valid || link_data != stalled_source_data) &&
                         !stalled_source_reported) begin
                overflow_toggle <= ~overflow_toggle;
                stalled_source_reported <= 1'b1;
            end
        end else begin
            stalled_source_active <= 1'b0;
            stalled_source_reported <= 1'b0;
        end
    end

    always @(posedge clk_24m or negedge local_rst_n) begin
        if (!local_rst_n) begin
            overflow_sync1 <= 1'b0;
            overflow_sync2 <= 1'b0;
            overflow_sync_last <= 1'b0;
            fifo_overflow_count <= 32'h0000_0000;
            max_fifo_level <= {(FIFO_ADDR_WIDTH+1){1'b0}};
            candidate_cycles <= 32'd0;
            last_frame_cycles <= 32'd0;
            heartbeat_age_cycles <= 32'h0000_0000;
        end else begin
            overflow_sync1 <= overflow_toggle;
            overflow_sync2 <= overflow_sync1;
            overflow_sync_last <= overflow_sync2;
            if (overflow_event_24m && fifo_overflow_count != 32'hffff_ffff)
                fifo_overflow_count <= fifo_overflow_count + 1'b1;
            if (fifo_rd_level > max_fifo_level)
                max_fifo_level <= fifo_rd_level;
            if (state_code == 0)
                candidate_cycles <= 32'd0;
            else if (state_code < 15 && candidate_cycles != 32'hffff_ffff)
                candidate_cycles <= candidate_cycles + 1'b1;
            if (good_frame_pulse)
                last_frame_cycles <= candidate_cycles;
            if (good_frame_pulse && good_frame_type == 8'h11)
                heartbeat_age_cycles <= 32'h0000_0000;
            else if (heartbeat_age_cycles != 32'hffff_ffff)
                heartbeat_age_cycles <= heartbeat_age_cycles + 1'b1;
        end
    end

    link_async_fifo #(
        .DATA_WIDTH(8),
        .ADDR_WIDTH(FIFO_ADDR_WIDTH)
    ) byte_fifo (
        .wr_clk(link_clk),
        .wr_rst_n(link_rst_n),
        .wr_en(link_valid && link_ready),
        .wr_data(link_data),
        .wr_full(fifo_full),
        .wr_level(fifo_wr_level),
        .rd_clk(clk_24m),
        .rd_rst_n(local_rst_n),
        .rd_en(fifo_read),
        .rd_data(fifo_data),
        .rd_empty(fifo_empty),
        .rd_level(fifo_rd_level)
    );

    link_frame_rx #(
        .TIMEOUT_CYCLES(FRAME_TIMEOUT_CYCLES)
    ) frame_rx (
        .clk(clk_24m),
        .rst_n(local_rst_n),
        .byte_valid(!fifo_empty),
        .byte_ready(parser_byte_ready),
        .byte_data(fifo_data),
        .frame_valid(frame_valid),
        .frame_ready(frame_ready),
        .frame_type(frame_type),
        .frame_seq(frame_seq),
        .frame_timestamp(frame_timestamp),
        .frame_length(frame_length),
        .payload_valid(payload_valid),
        .payload_ready(payload_ready),
        .payload_data(payload_data),
        .payload_last(payload_last),
        .good_frame_pulse(good_frame_pulse),
        .good_frame_type(good_frame_type),
        .busy(busy),
        .state_code(state_code),
        .good_frame_count(good_frame_count),
        .byte_count(rx_byte_count),
        .crc_error_count(crc_error_count),
        .sequence_gap_count(sequence_gap_count),
        .length_error_count(length_error_count),
        .version_error_count(version_error_count),
        .timeout_count(timeout_count),
        .selftest_error_count(selftest_error_count),
        .last_error_code(last_error_code)
    );

    wire unused_fifo_wr_level;
    assign unused_fifo_wr_level = ^fifo_wr_level;
endmodule

`default_nettype wire

`timescale 1ns/1ps

module uart_tx_byte #(
    parameter integer CLKS_PER_BIT = 24
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output reg        tx,
    output reg        busy,
    output reg        done
);

    reg [7:0] data_latched;
    reg [3:0] phase;
    reg [15:0] clock_count;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            data_latched <= 8'd0;
            phase       <= 4'd0;
            clock_count <= 16'd0;
            tx          <= 1'b1;
            busy        <= 1'b0;
            done        <= 1'b0;
        end else begin
            done <= 1'b0;

            if (!busy) begin
                tx          <= 1'b1;
                phase       <= 4'd0;
                clock_count <= 16'd0;

                if (start) begin
                    data_latched <= data;
                    tx           <= 1'b0;
                    busy         <= 1'b1;
                end
            end else if (clock_count == CLKS_PER_BIT - 1) begin
                clock_count <= 16'd0;
                case (phase)
                    4'd0: begin tx <= data_latched[0]; phase <= 4'd1; end
                    4'd1: begin tx <= data_latched[1]; phase <= 4'd2; end
                    4'd2: begin tx <= data_latched[2]; phase <= 4'd3; end
                    4'd3: begin tx <= data_latched[3]; phase <= 4'd4; end
                    4'd4: begin tx <= data_latched[4]; phase <= 4'd5; end
                    4'd5: begin tx <= data_latched[5]; phase <= 4'd6; end
                    4'd6: begin tx <= data_latched[6]; phase <= 4'd7; end
                    4'd7: begin tx <= data_latched[7]; phase <= 4'd8; end
                    4'd8: begin tx <= 1'b1;            phase <= 4'd9; end
                    default: begin
                        tx    <= 1'b1;
                        busy  <= 1'b0;
                        done  <= 1'b1;
                        phase <= 4'd0;
                    end
                endcase
            end else begin
                clock_count <= clock_count + 1'b1;
            end
        end
    end

endmodule

`timescale 1ns/1ps
`default_nettype none
// Native 1920x1080@60 timing, 148.5MHz. Existing 640x480 canvas is
// displayed 3x horizontally and 9/4 vertically, filling all active scan lines.
// This first experiment establishes clock/timing feasibility, not new artwork detail.
module vga_timing(input wire clk,rst_n,
 output reg [11:0] h,v,output wire hs,vs,de,
 output wire [9:0] x,output wire [8:0] y,output reg frame_start,
 output reg [8:0] wave_x,output reg [4:0] wave_phase);
 reg wave_window;
 reg x_window;
 reg [1:0] x_repeat;reg [9:0] logical_x;
 reg [3:0] y_phase;reg [8:0] logical_y;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin x_window<=0;wave_window<=0;frame_start<=1;h<=0;v<=0;x_repeat<=0;logical_x<=0;y_phase<=0;logical_y<=0;wave_phase<=0;wave_x<=0;end
  else begin
   x_window<=h>=191 && h<2110;
   wave_window<=h>=383 && h<2110;
   frame_start<=h==2199 && v==1124;
   if(h==2199)begin
    h<=0;v<=v==1124?0:v+1'b1;
    if(v<41 || v>=1120)begin logical_y<=0;y_phase<=0;end
    else if(y_phase>=5)begin y_phase<=y_phase-5;logical_y<=logical_y+1'b1;end
    else y_phase<=y_phase+4;
   end
   else h<=h+1'b1;
   if(!x_window)begin x_repeat<=0;logical_x<=0;end
   else if(x_repeat==2)begin x_repeat<=0;logical_x<=logical_x+1'b1;end
   else x_repeat<=x_repeat+1'b1;
   // 512 history points over 1728 physical pixels: floor((h-384)*8/27).
   if(!wave_window)begin wave_phase<=0;wave_x<=0;end
   else if(wave_phase>=19)begin wave_phase<=wave_phase-19;wave_x<=wave_x+1'b1;end
   else wave_phase<=wave_phase+8;
  end
 assign hs=(h<44);assign vs=(v<5); // positive H/V sync for this mode
 assign de=(h>=192 && h<2112 && v>=41 && v<1121);
 assign x=logical_x;assign y=logical_y;

endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// EG4 PLL: EXPERIMENT: 24 MHz / 2 * 99 / 8 = 148.5 MHz, VCO = 1188 MHz.
// VCO feedback avoids a low phase-detector frequency. Analog parameters are
// from the official 972 MHz VCO example; physical lock remains board testing.
module vga_pll_25m2(input wire refclk,reset,output wire clk_pix,clk_dac,locked);
 wire [4:0] clocks;
 EG_LOGIC_BUFG pixel_buf(.i(clocks[0]),.o(clk_pix));
 EG_LOGIC_BUFG dac_buf(.i(clocks[1]),.o(clk_dac));
 EG_PHY_PLL #(.FIN("24.000"),.REFCLK_DIV(2),.FBCLK_DIV(99),
  .CLKC1_DIV(8),.CLKC1_ENABLE("ENABLE"),.CLKC1_CPHASE(2),.CLKC1_FPHASE(0),
  .CLKC0_DIV(8),.CLKC0_ENABLE("ENABLE"),.CLKC0_CPHASE(7),.CLKC0_FPHASE(0),
  .FEEDBK_MODE("NORMAL"),.FEEDBK_PATH("VCO_PHASE_0"),
  .STDBY_ENABLE("DISABLE"),.PLLRST_ENA("ENABLE"),.SYNC_ENABLE("DISABLE"),
  .GMC_GAIN(0),.ICP_CURRENT(9),.KVCO(2),.LPF_CAPACITOR(2),.LPF_RESISTOR(8),
  .DERIVE_PLL_CLOCKS("DISABLE"),.GEN_BASIC_CLOCK("DISABLE")) pll(
  .refclk(refclk),.reset(reset),.stdby(1'b0),.extlock(locked),
  // VCO_PHASE_0 selects internal VCO feedback; external fbclk is unused.
  .fbclk(1'b0),.clkc(clocks),.load_reg(1'b0),.psclk(1'b0),.psdown(1'b0),
  .psstep(1'b0),.psclksel(3'b0),.psdone(),.dclk(1'b0),.dcs(1'b0),
  .dwe(1'b0),.di(8'b0),.daddr(6'b0),.\do ());
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Same glyphs as shared vga_font, with a synchronous row ROM for 148.5MHz.
module pcs_font_row(input wire clk,input wire [6:0] character,input wire [2:0] row,output reg [4:0] row_bits);
 (* ram_style="block" *) reg [4:0] rows[0:1023];
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/gesture_tasks/assets/font_rows.hex",rows);
 always @(posedge clk)row_bits<=rows[{character,row}];
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Three-clock packed ROM pipeline. Explicit local registers isolate the slow
// 32K RAM outputs from bank/nibble selection at the 148.5MHz pixel clock.
module b_v2_whale_sprite(input wire clk,input wire [4:0] frame,
 input wire [5:0] x,y,output wire [23:0] rgb);
 wire [11:0] address={frame[0],y,x[5:1]};
 (* ram_style="bram_32k" *) reg [7:0] packed0[0:4095];
 reg [7:0] q0;(* preserve="true" *) reg [7:0] delay0;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed0.hex",packed0);
 always @(posedge clk)begin q0<=packed0[address];delay0<=q0;end
 (* ram_style="bram_32k" *) reg [7:0] packed1[0:4095];
 reg [7:0] q1;(* preserve="true" *) reg [7:0] delay1;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed1.hex",packed1);
 always @(posedge clk)begin q1<=packed1[address];delay1<=q1;end
 (* ram_style="bram_32k" *) reg [7:0] packed2[0:4095];
 reg [7:0] q2;(* preserve="true" *) reg [7:0] delay2;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed2.hex",packed2);
 always @(posedge clk)begin q2<=packed2[address];delay2<=q2;end
 (* ram_style="bram_32k" *) reg [7:0] packed3[0:4095];
 reg [7:0] q3;(* preserve="true" *) reg [7:0] delay3;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed3.hex",packed3);
 always @(posedge clk)begin q3<=packed3[address];delay3<=q3;end
 (* ram_style="bram_32k" *) reg [7:0] packed4[0:4095];
 reg [7:0] q4;(* preserve="true" *) reg [7:0] delay4;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed4.hex",packed4);
 always @(posedge clk)begin q4<=packed4[address];delay4<=q4;end
 (* ram_style="bram_32k" *) reg [7:0] packed5[0:4095];
 reg [7:0] q5;(* preserve="true" *) reg [7:0] delay5;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed5.hex",packed5);
 always @(posedge clk)begin q5<=packed5[address];delay5<=q5;end
 (* ram_style="bram_32k" *) reg [7:0] packed6[0:4095];
 reg [7:0] q6;(* preserve="true" *) reg [7:0] delay6;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed6.hex",packed6);
 always @(posedge clk)begin q6<=packed6[address];delay6<=q6;end
 (* ram_style="bram_32k" *) reg [7:0] packed7[0:4095];
 reg [7:0] q7;(* preserve="true" *) reg [7:0] delay7;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed7.hex",packed7);
 always @(posedge clk)begin q7<=packed7[address];delay7<=q7;end
 (* ram_style="bram_32k" *) reg [7:0] packed8[0:4095];
 reg [7:0] q8;(* preserve="true" *) reg [7:0] delay8;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed8.hex",packed8);
 always @(posedge clk)begin q8<=packed8[address];delay8<=q8;end
 (* ram_style="bram_32k" *) reg [7:0] packed9[0:4095];
 reg [7:0] q9;(* preserve="true" *) reg [7:0] delay9;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed9.hex",packed9);
 always @(posedge clk)begin q9<=packed9[address];delay9<=q9;end
 (* ram_style="bram_32k" *) reg [7:0] packed10[0:4095];
 reg [7:0] q10;(* preserve="true" *) reg [7:0] delay10;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed10.hex",packed10);
 always @(posedge clk)begin q10<=packed10[address];delay10<=q10;end
 (* ram_style="bram_32k" *) reg [7:0] packed11[0:4095];
 reg [7:0] q11;(* preserve="true" *) reg [7:0] delay11;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/ab_scope_v2/assets/whale/packed11.hex",packed11);
 always @(posedge clk)begin q11<=packed11[address];delay11<=q11;end
 reg [3:0] bank1;(* preserve="true" *) reg [23:0] bank2;reg half1,half2;reg [3:0] palette_index;
 reg [7:0] selected;
 always @*selected=({8{bank2[0]}} & delay0)|({8{bank2[1]}} & delay1)|({8{bank2[2]}} & delay2)|({8{bank2[3]}} & delay3)|({8{bank2[4]}} & delay4)|({8{bank2[5]}} & delay5)|({8{bank2[6]}} & delay6)|({8{bank2[7]}} & delay7)|({8{bank2[8]}} & delay8)|({8{bank2[9]}} & delay9)|({8{bank2[10]}} & delay10)|({8{bank2[11]}} & delay11);
 always @(posedge clk)begin
  bank1<=frame[4:1];bank2<=24'b1<<({bank1,1'b0}+half1);half1<=x[0];half2<=half1;
  palette_index<=({4{bank2[0]}} & delay0[3:0])|({4{bank2[1]}} & delay0[7:4])|({4{bank2[2]}} & delay1[3:0])|({4{bank2[3]}} & delay1[7:4])|({4{bank2[4]}} & delay2[3:0])|({4{bank2[5]}} & delay2[7:4])|({4{bank2[6]}} & delay3[3:0])|({4{bank2[7]}} & delay3[7:4])|({4{bank2[8]}} & delay4[3:0])|({4{bank2[9]}} & delay4[7:4])|({4{bank2[10]}} & delay5[3:0])|({4{bank2[11]}} & delay5[7:4])|({4{bank2[12]}} & delay6[3:0])|({4{bank2[13]}} & delay6[7:4])|({4{bank2[14]}} & delay7[3:0])|({4{bank2[15]}} & delay7[7:4])|({4{bank2[16]}} & delay8[3:0])|({4{bank2[17]}} & delay8[7:4])|({4{bank2[18]}} & delay9[3:0])|({4{bank2[19]}} & delay9[7:4])|({4{bank2[20]}} & delay10[3:0])|({4{bank2[21]}} & delay10[7:4])|({4{bank2[22]}} & delay11[3:0])|({4{bank2[23]}} & delay11[7:4]);
 end
 localparam [15:0] P0=16'hc3d6; assign rgb[0]=P0[palette_index];
 localparam [15:0] P1=16'hc41c; assign rgb[1]=P1[palette_index];
 localparam [15:0] P2=16'hc2b2; assign rgb[2]=P2[palette_index];
 localparam [15:0] P3=16'h3c2f; assign rgb[3]=P3[palette_index];
 localparam [15:0] P4=16'hfe13; assign rgb[4]=P4[palette_index];
 localparam [15:0] P5=16'h023e; assign rgb[5]=P5[palette_index];
 localparam [15:0] P6=16'h01ee; assign rgb[6]=P6[palette_index];
 localparam [15:0] P7=16'h00fe; assign rgb[7]=P7[palette_index];
 localparam [15:0] P8=16'h0298; assign rgb[8]=P8[palette_index];
 localparam [15:0] P9=16'h81d2; assign rgb[9]=P9[palette_index];
 localparam [15:0] P10=16'h7e4f; assign rgb[10]=P10[palette_index];
 localparam [15:0] P11=16'hfcbb; assign rgb[11]=P11[palette_index];
 localparam [15:0] P12=16'h026e; assign rgb[12]=P12[palette_index];
 localparam [15:0] P13=16'h0126; assign rgb[13]=P13[palette_index];
 localparam [15:0] P14=16'h00ee; assign rgb[14]=P14[palette_index];
 localparam [15:0] P15=16'h001e; assign rgb[15]=P15[palette_index];
 localparam [15:0] P16=16'hab20; assign rgb[16]=P16[palette_index];
 localparam [15:0] P17=16'ha142; assign rgb[17]=P17[palette_index];
 localparam [15:0] P18=16'ha1f6; assign rgb[18]=P18[palette_index];
 localparam [15:0] P19=16'h5e5f; assign rgb[19]=P19[palette_index];
 localparam [15:0] P20=16'h0196; assign rgb[20]=P20[palette_index];
 localparam [15:0] P21=16'h0076; assign rgb[21]=P21[palette_index];
 localparam [15:0] P22=16'h001e; assign rgb[22]=P22[palette_index];
 localparam [15:0] P23=16'h000e; assign rgb[23]=P23[palette_index];
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// GIF timestamps rounded cumulatively to video frames; update only at vblank.
module b_v2_whale_player(input wire clk,rst_n,frame_start,output reg [4:0] frame);
 function [4:0] hold_ticks;input [4:0] n;
  begin case(n)
   5'd0:hold_ticks=5'd7;
   5'd1:hold_ticks=5'd6;
   5'd2:hold_ticks=5'd7;
   5'd3:hold_ticks=5'd6;
   5'd4:hold_ticks=5'd7;
   5'd5:hold_ticks=5'd7;
   5'd6:hold_ticks=5'd7;
   5'd7:hold_ticks=5'd8;
   5'd8:hold_ticks=5'd8;
   5'd9:hold_ticks=5'd10;
   5'd10:hold_ticks=5'd10;
   5'd11:hold_ticks=5'd14;
   5'd12:hold_ticks=5'd19;
   5'd13:hold_ticks=5'd25;
   5'd14:hold_ticks=5'd19;
   5'd15:hold_ticks=5'd10;
   5'd16:hold_ticks=5'd9;
   5'd17:hold_ticks=5'd8;
   5'd18:hold_ticks=5'd7;
   5'd19:hold_ticks=5'd8;
   5'd20:hold_ticks=5'd7;
   5'd21:hold_ticks=5'd7;
   5'd22:hold_ticks=5'd7;
   5'd23:hold_ticks=5'd8;
   default:hold_ticks=5'd7;
  endcase end
 endfunction
 reg [4:0] remaining;
 wire [4:0] next_frame=frame==23?5'd0:frame+1'b1;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin frame<=0;remaining<=5'd7;end
  else if(frame_start)begin
   if(remaining==1)begin frame<=next_frame;remaining<=hold_ticks(next_frame);end
   else remaining<=remaining-1'b1;
  end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Input has already passed the shared outer CRC parser. Commit whole payloads only.
module b_v2_decode #(parameter integer STALE_CLKS=7200000)(
 input wire clk,rst_n,frame_valid,output wire frame_ready,
 input wire [7:0] frame_type,input wire [10:0] frame_length,
 input wire payload_valid,output wire payload_ready,input wire [7:0] payload_data,input wire payload_last,
 output reg wave_valid,result_valid,segment_break,output reg [127:0] raw_mv,
 output reg [191:0] filtered_q8,fast_q4,slow_q4,
 output reg [31:0] sample_seq,window_seq,cnn_cycles,head_cycles,
 output reg [15:0] sample_rate,generation,epoch,class_id,class_count,action_hint,margin,
 output reg [7:0] wave_flags,result_flags,
 output wire stale,output reg [31:0] accepted,rejected,breaks,
 output reg result_fault,output reg [31:0] reject_info,output reg [255:0] rejected_payload
);
 reg collecting,finalize;reg [7:0] kind;reg [10:0] length,index;
 reg [831:0] bytes;
 // Payload storage has no reset value: a complete 104/32-byte transaction
 // overwrites every bit used by its decoder before finalize can commit it.
 always @(posedge clk)if(payload_valid && payload_ready)bytes<={payload_data,bytes[831:8]};
 wire [831:0] complete=bytes;
 // Short result payload ends in the high 256 bits of the shift register.
 wire [255:0] r=complete[831:576];
 reg wave_seen,result_seen;reg [31:0] age;reg [15:0] wave_gen,wave_epoch,result_gen,result_epoch;
 wire wave_ok=complete[7:0]==1 && complete[15:8]==15 && complete[23:16]==8 &&
  complete[31:27]==0 && complete[127:112]==0 && complete[79:64]!=0;
 wire result_ok=r[7:0]==1 && r[15:10]==0 && r[159:144]==0 && r[255:224]==0 &&
  r[63:48]<=256 && r[127:112]!=0 && (r[31:16]==16'hffff || r[31:16]<r[127:112]);
 wire [7:0] reject_reasons={
  (r[127:112]==0 || (r[31:16]!=16'hffff && r[31:16]>=r[127:112])),
  r[63:48]>256,(r[159:144]!=0 || r[255:224]!=0),(r[7:0]!=1 || r[15:10]!=0),
  !lanes_ok,!wave_ok,(kind!=8'h40 && kind!=8'h41 && kind!=8'h11),
  (index!=length || (kind==8'h40 && length!=104) || (kind==8'h41 && length!=32))};
 wire rejecting=finalize && (index!=length ||
  (kind==8'h40 ? !(length==104 && wave_ok && lanes_ok) :
   kind==8'h41 ? !(length==32 && result_ok) : kind!=8'h11));
 reg lanes_ok;integer k;
 always @*begin
  lanes_ok=1;
  for(k=0;k<8;k=k+1)begin
   if(complete[128+k*16+:16]>3300)lanes_ok=0;
   if(complete[448+k*24+:24]>24'h0fffff || complete[640+k*24+:24]>24'h0fffff)lanes_ok=0;
  end
 end
 assign frame_ready=!collecting;assign payload_ready=collecting && !finalize;
 assign stale=age>=STALE_CLKS;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   collecting<=0;finalize<=0;kind<=0;length<=0;index<=0;
   wave_valid<=0;result_valid<=0;segment_break<=0;raw_mv<=0;filtered_q8<=0;fast_q4<=0;slow_q4<=0;
   sample_seq<=0;window_seq<=0;cnn_cycles<=0;head_cycles<=0;sample_rate<=0;generation<=0;epoch<=0;
   class_id<=16'hffff;class_count<=0;action_hint<=16'hffff;margin<=0;wave_flags<=0;result_flags<=0;
   wave_seen<=0;result_seen<=0;wave_gen<=0;wave_epoch<=0;result_gen<=0;result_epoch<=0;
   age<=STALE_CLKS;accepted<=0;rejected<=0;breaks<=0;
   result_fault<=0;reject_info<=0;rejected_payload<=0;
  end else begin
   wave_valid<=0;result_valid<=0;segment_break<=0;result_fault<=0;
   if(rejecting)begin
    reject_info<={kind,reject_reasons,5'd0,length};rejected_payload<=r;
    if(kind==8'h41)begin
     // A CRC-valid but malformed classification invalidates motion authority.
     // It does not invalidate the independently sequenced waveform stream.
     result_fault<=1;result_seen<=0;result_flags<=0;class_id<=16'hffff;
    end else begin segment_break<=1;breaks<=breaks+1'b1;end
   end
   if(!stale)age<=age+1'b1;
   if(age==STALE_CLKS-1)begin wave_seen<=0;result_seen<=0;segment_break<=1;breaks<=breaks+1'b1;end
   if(frame_valid && frame_ready)begin
    kind<=frame_type;length<=frame_length;index<=0;collecting<=frame_length!=0;
   end
   if(payload_valid && payload_ready)begin
    index<=index+1'b1;
    if(payload_last)finalize<=1;
   end
   if(finalize)begin
     collecting<=0;finalize<=0;
     if(index!=length)rejected<=rejected+1'b1;
     else if(kind==8'h40 && length==104 && wave_ok && lanes_ok)begin
      raw_mv<=complete[255:128];filtered_q8<=complete[447:256];fast_q4<=complete[639:448];slow_q4<=complete[831:640];
      sample_seq<=complete[63:32];sample_rate<=complete[79:64];wave_flags<=complete[31:24];
      generation<=complete[95:80];epoch<=complete[111:96];wave_gen<=complete[95:80];wave_epoch<=complete[111:96];
      if(!wave_seen || stale || complete[24] || complete[63:32]!=sample_seq+32'd1 ||
       complete[95:80]!=wave_gen || complete[111:96]!=wave_epoch ||
       (result_seen && (complete[95:80]!=result_gen || complete[111:96]!=result_epoch)))begin
       segment_break<=1;breaks<=breaks+1'b1;
      end
      wave_seen<=1;age<=0;wave_valid<=1;accepted<=accepted+1'b1;
     end else if(kind==8'h41 && length==32 && result_ok)begin
      class_id<=r[31:16];action_hint<=r[47:32];margin<=r[63:48];window_seq<=r[95:64];
      generation<=r[111:96];class_count<=r[127:112];epoch<=r[143:128];result_flags<=r[15:8];
      cnn_cycles<=r[191:160];head_cycles<=r[223:192];result_gen<=r[111:96];result_epoch<=r[143:128];
      if((wave_seen && (r[111:96]!=wave_gen || r[143:128]!=wave_epoch)) ||
       (result_seen && (r[111:96]!=result_gen || r[143:128]!=result_epoch)))begin
       segment_break<=1;wave_seen<=0;breaks<=breaks+1'b1;
      end
      result_seen<=1;age<=0;result_valid<=1;accepted<=accepted+1'b1;
     end else if(kind!=8'h11)rejected<=rejected+1'b1;
   end
  end
 end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Full signed16 history, Q8 amplitude, pixel-square/one-pixel-strip area coverage.
// 256 coverage levels with linear-light sRGB blending; stages 0..18.
module pcs_native_trace(
 input wire clk,rst_n,sample_valid,
 input wire [127:0] sample_data,
 input wire [9:0] write_ptr,view_base,
 input wire raw_page,
 input wire mean_read,input wire [8:0] mean_even_addr,mean_odd_addr,
 output wire [127:0] mean_even_data,mean_odd_data,
 input wire [127:0] centers,ranges,gains,input wire [7:0] custom,
 input wire wave_valid,view_commit,history_clear,output reg clipped,
 input wire [11:0] h,v,
 input wire [8:0] sample_index,
 input wire [4:0] sample_phase,
 output reg [23:0] rgb,
 output reg active
);
 (* ram_style="block" *) reg [127:0] ring_even[0:511];
 (* ram_style="block" *) reg [127:0] ring_odd[0:511];
 (* ram_style="block" *) reg [23:0] normalization[0:1023];
 (* ram_style="bram_32k" *) reg [7:0] coverage[0:8191];
 (* ram_style="block" *) reg [23:0] colours[0:2047];
 initial begin
  $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/pc_scope_1080_fine_exp/assets/normalization.hex",normalization);
  $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/pc_scope_1080_fine_exp/assets/coverage.hex",coverage);
  $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/pc_scope_1080_fine_exp/assets/trace_colours.hex",colours);
 end
 reg [8:0] index0;
 wire [7:0] band_decode={v>=779,v>=689 && v<779,v>=599 && v<689,v>=509 && v<599,v>=419 && v<509,v>=329 && v<419,v>=239 && v<329,v<239};
 wire [2:0] channel_decode={|band_decode[7:4],|(band_decode & 8'hcc),|(band_decode & 8'haa)};
 reg [7:0] band0;
 reg [6:0] lane_candidates0[0:7];
 always @(posedge clk)begin
  band0<=band_decode;
  lane_candidates0[0]<=v[6:0]-7'd21;
  lane_candidates0[1]<=v[6:0]-7'd111;
  lane_candidates0[2]<=v[6:0]-7'd73;
  lane_candidates0[3]<=v[6:0]-7'd35;
  lane_candidates0[4]<=v[6:0]-7'd125;
  lane_candidates0[5]<=v[6:0]-7'd87;
  lane_candidates0[6]<=v[6:0]-7'd49;
  lane_candidates0[7]<=v[6:0]-7'd11;
 end
 reg [2:0] channel[0:20];reg [6:0] lane[0:9];
 reg [4:0] phase[0:9];reg [5:0] inv_phase[0:9];
 reg [20:0] region_pipe,valid_pipe,mid_grid,edge_grid;
 integer i;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin
   region_pipe<=0;valid_pipe<=0;mid_grid<=0;edge_grid<=0;
  end else begin
   region_pipe<={region_pipe[19:0],h>=384 && h<2112 && v>=149 && v<869};
   valid_pipe<={valid_pipe[19:0],wave_valid};
   mid_grid<={mid_grid[19:0],(v==194 || v==284 || v==374 || v==464 || v==554 || v==644 || v==734 || v==824) || (sample_index[5:0]==0 && sample_phase==0)};
   edge_grid<={edge_grid[19:0],(v==149 || v==239 || v==329 || v==419 || v==509 || v==599 || v==689 || v==779)};
  end
 // Data-only pipelines flush before region/valid can reach the output.
 // Removing their asynchronous resets reduces recovery fanout at 148.5MHz.
 integer data_i;
 always @(posedge clk)begin
  index0<=sample_index;channel[0]<=channel_decode;
  phase[0]<=sample_phase;inv_phase[0]<=6'd27-{1'b0,sample_phase};
  for(data_i=1;data_i<21;data_i=data_i+1)channel[data_i]<=channel[data_i-1];
  lane[1]<=({7{band0[0]}} & lane_candidates0[0]) | ({7{band0[1]}} & lane_candidates0[1]) | ({7{band0[2]}} & lane_candidates0[2]) | ({7{band0[3]}} & lane_candidates0[3]) | ({7{band0[4]}} & lane_candidates0[4]) | ({7{band0[5]}} & lane_candidates0[5]) | ({7{band0[6]}} & lane_candidates0[6]) | ({7{band0[7]}} & lane_candidates0[7]);
  for(data_i=2;data_i<10;data_i=data_i+1)lane[data_i]<=lane[data_i-1];
  for(data_i=1;data_i<10;data_i=data_i+1)begin phase[data_i]<=phase[data_i-1];inv_phase[data_i]<=inv_phase[data_i-1];end
 end
 // Split the modulo-1024 sum by parity. Even/odd RAM addresses are
 // ceil/floor((view_base+index0)/2); avoid add10 -> increment -> mux.
 wire [8:0] half_index={1'b0,index0[8:1]};
 // Append carry-in as a low bit: ONE adder, no conditional increment.
 wire [9:0] odd_sum={view_base[9:1],1'b1}+{half_index,(view_base[0] & index0[0])};
 wire [9:0] even_sum={view_base[9:1],1'b1}+{half_index,(view_base[0] | index0[0])};
 wire [8:0] odd_address=odd_sum[9:1],even_address=even_sum[9:1];
 wire address_odd=view_base[0]^index0[0];
 assign mean_even_data=even1;assign mean_odd_data=odd1;
 reg [127:0] even1,odd1,data_a2,data_b2;reg odd_first1;
 reg last1,last2;
 reg signed [15:0] sample_a3,sample_b3;
 reg signed [40:0] scale_a4,scale_b4;
 reg signed [33:0] scale_low_a4,scale_low_b4;
 reg signed [23:0] scale_high_a4,scale_high_b4;
 reg [23:0] coef3;
 reg [23:0] lane_coef[0:7];
 reg [2:0] cfg_lane,cfg_lane1,cfg_lane2;
 reg [15:0] cfg_gain,cfg_gain2,cfg_range;
 reg [23:0] cfg_standard;
 reg [5:0] cfg_match;
 reg [2:0] cfg_lane3;reg [15:0] cfg_gain3;reg cfg_custom3;
 reg cfg_custom,cfg_custom2;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin cfg_lane<=0;cfg_lane1<=0;cfg_lane2<=0;cfg_gain<=0;cfg_gain2<=0;cfg_range<=0;cfg_match<=0;cfg_standard<=0;cfg_custom<=0;cfg_custom2<=0;cfg_lane3<=0;cfg_gain3<=0;cfg_custom3<=0;end
  else begin
   cfg_lane<=cfg_lane+1'b1;cfg_lane1<=cfg_lane;cfg_lane2<=cfg_lane1;
   cfg_gain<=gains[cfg_lane*16+:16];cfg_range<=ranges[cfg_lane*16+:16];cfg_custom<=custom[cfg_lane];
   cfg_match<={cfg_range==1200,cfg_range==500,
    cfg_range==200,cfg_range==100,
    cfg_range==50,cfg_range==20};
   cfg_standard<=({24{cfg_match[0]}} & 24'd2149581) |
    ({24{cfg_match[1]}} & 24'd859832) | ({24{cfg_match[2]}} & 24'd429916) |
    ({24{cfg_match[3]}} & 24'd214958) | ({24{cfg_match[4]}} & 24'd85983) |
    ({24{cfg_match[5]}} & 24'd35826) | ({24{!(|cfg_match)}} & 24'd13028);cfg_gain2<=cfg_gain;cfg_custom2<=cfg_custom;
   cfg_lane3<=cfg_lane2;cfg_gain3<=cfg_gain2;cfg_custom3<=cfg_custom2;
  end
 always @(posedge clk)lane_coef[cfg_lane3]<=cfg_custom3?cfg_gain3*24'd117:cfg_standard;
 reg [15:0] lane_a2,lane_b2,center2;
 reg [127:0] even_ram2,odd_ram2;
 reg odd_ramfirst2,last_ram2;
 always @(posedge clk)begin
  even_ram2<=even1;odd_ram2<=odd1;odd_ramfirst2<=odd_first1;last_ram2<=last1;
 end
 reg [15:0] even_read2,odd_read2;reg odd_first2,last_read2;
 always @(posedge clk)begin
  even_read2<=even_ram2[channel[2]*16+:16];odd_read2<=odd_ram2[channel[2]*16+:16];odd_first2<=odd_ramfirst2;last_read2<=last_ram2;
 end
 always @(posedge clk)begin
  lane_a2<=odd_first2?odd_read2:even_read2;
  lane_b2<=last_read2?(odd_first2?odd_read2:even_read2):(odd_first2?even_read2:odd_read2);
  center2<=centers[channel[3]*16+:16];
 end
 function [23:0] raw_coef;input [15:0] r;
  begin case(r)
   20:raw_coef=2149581;50:raw_coef=859832;100:raw_coef=429916;
   200:raw_coef=214958;500:raw_coef=85983;1200:raw_coef=35826;default:raw_coef=13028;
  endcase end
 endfunction
 reg signed [14:0] q_a5,q_b5;
 reg high_a5,low_a5,high_b5,low_b5;
 reg clip_accum;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin clip_accum<=0;clipped<=0;end
  else if(history_clear)begin clip_accum<=0;clipped<=0;end
  else if(view_commit)begin clipped<=clip_accum;clip_accum<=0;end
  else if(region_pipe[8] && valid_pipe[8] && (high_a5 || low_a5 || high_b5 || low_b5))clip_accum<=1;
 end
 reg [14:0] amplitude_a6,amplitude_b6;
 reg [19:0] product_a7,product_b7,row7,numerator8,row8;
 reg signed [15:0] slope7;
 reg [15:0] slope8;
 wire [16:0] rounded_slope={1'b0,slope8}+17'd16;
 reg signed [20:0] error9;
 reg [23:0] normal9,normal10;
 reg [20:0] magnitude10;
 reg [35:0] low11;reg [36:0] low12;
 reg [17:0] cross_a11;reg [20:0] cross_b11;reg [2:0] high11;
 reg [21:0] cross12;
 reg [39:0] distance13;
 wire [40:0] rounded_distance={1'b0,distance13}+41'd8388608;
 reg [4:0] angle11,angle12,angle13,angle14;
 reg [7:0] distance14;
 reg [7:0] coverage15,coverage16;
 reg [23:0] colour17;
 reg line17;
 always @(posedge clk)begin
  if(sample_valid && !write_ptr[0])ring_even[write_ptr[9:1]]<=sample_data;
  if(sample_valid && write_ptr[0])ring_odd[write_ptr[9:1]]<=sample_data;
  even1<=ring_even[mean_read?mean_even_addr:even_address];odd1<=ring_odd[mean_read?mean_odd_addr:odd_address];odd_first1<=address_odd;last1<=index0==511;
  data_a2<=odd_first1?odd1:even1;data_b2<=odd_first1?even1:odd1;last2<=last1;
  sample_a3<=raw_page?$signed({1'b0,lane_a2})-$signed({1'b0,center2}):lane_a2;
  coef3<=raw_page?lane_coef[channel[4]]:24'd1872;
  sample_b3<=raw_page?$signed({1'b0,lane_b2})-$signed({1'b0,center2}):lane_b2;
  scale_low_a4<=sample_a3*$signed({1'b0,coef3[16:0]});
  scale_low_b4<=sample_b3*$signed({1'b0,coef3[16:0]});
  scale_high_a4<=sample_a3*$signed({1'b0,coef3[23:17]});
  scale_high_b4<=sample_b3*$signed({1'b0,coef3[23:17]});
  scale_a4<={{7{scale_low_a4[33]}},scale_low_a4}+{scale_high_a4,17'd0};
  scale_b4<={{7{scale_low_b4[33]}},scale_low_b4}+{scale_high_b4,17'd0};
  q_a5<=($signed(scale_a4)>>>12);q_b5<=($signed(scale_b4)>>>12);
  high_a5<=$signed(($signed(scale_a4)>>>12))>15'sd10496;low_a5<=$signed(($signed(scale_a4)>>>12))< -15'sd10496;
  high_b5<=$signed(($signed(scale_b4)>>>12))>15'sd10496;low_b5<=$signed(($signed(scale_b4)>>>12))< -15'sd10496;
  amplitude_a6<=high_a5?15'd1024:low_a5?15'd22016:(15'd11520-q_a5);
  amplitude_b6<=high_b5?15'd1024:low_b5?15'd22016:(15'd11520-q_b5);
  product_a7<=amplitude_a6*inv_phase[9];product_b7<=amplitude_b6*phase[9];
  row7<=lane[9]*20'd6912;
  slope7<=$signed({1'b0,amplitude_b6})-$signed({1'b0,amplitude_a6});
  numerator8<=product_a7+product_b7;row8<=row7;
  slope8<=slope7[15]?-slope7:slope7;
  error9<=$signed({1'b0,numerator8})-$signed({1'b0,row8});
  normal9<=normalization[(slope8[14:5]+{9'd0,slope8[4]})];
  magnitude10<=error9[20]?-error9:error9;normal10<=normal9;
  // Split the 21x19 product at registered partial products; no cascaded
  // wide multiply/add path must finish within a single 148.5MHz cycle.
  low11<=magnitude10[17:0]*normal10[17:0];
  cross_a11<=normal10[18]?magnitude10[17:0]:18'd0;
  cross_b11<=magnitude10[20:18]*normal10[17:0];
  high11<=normal10[18]?magnitude10[20:18]:3'd0;
  angle11<=normal10[23:19];
  low12<={1'b0,low11}+37'd8388608;cross12<={4'd0,cross_a11}+{1'b0,cross_b11}+{1'b0,high11,18'd0};angle12<=angle11;
  distance13<={3'd0,low12}+{cross12,18'd0};angle13<=angle12;
  distance14<=|distance13[39:32]?8'd255:distance13[31:24];angle14<=angle13;
  coverage15<=coverage[{angle14,distance14}];
  coverage16<=coverage15;
  colour17<=colours[{channel[19],coverage16}];line17<=coverage16!=0;
 end
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin rgb<=0;active<=0;end
  else begin
   active<=region_pipe[20];
   if(line17 && valid_pipe[20])rgb<=colour17;
   else if(edge_grid[20])rgb<=24'h304058;
   else if(mid_grid[20])rgb<=24'h203044;
   else rgb<=24'h080c18;
  end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// One digit converter, two values. Runs for 32 clocks during vertical blanking.
module b_v2_ui_numbers(input wire clk,rst_n,update,input wire [15:0] rate,
 input wire [9:0] fill,output reg [19:0] rate_bcd,output reg [11:0] fill_bcd);
 reg busy,phase;reg [4:0] step;reg [15:0] binary,fill_hold;reg [19:0] digits;
 reg [19:0] adjusted;integer i;
 always @*begin
  adjusted=digits;
  for(i=0;i<5;i=i+1)if(digits[i*4+:4]>=5)adjusted[i*4+:4]=digits[i*4+:4]+3;
 end
 wire [19:0] shifted={adjusted[18:0],binary[15]};
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin busy<=0;phase<=0;step<=0;binary<=0;digits<=0;fill_hold<=0;rate_bcd<=0;fill_bcd<=0;end
  else if(update && !busy)begin busy<=1;phase<=0;step<=0;binary<=rate;fill_hold<={6'd0,fill};digits<=0;end
  else if(busy)begin
   binary<={binary[14:0],1'b0};digits<=shifted;step<=step+1'b1;
   if(step==15)begin
    if(!phase)begin rate_bcd<=shifted;phase<=1;step<=0;binary<=fill_hold;digits<=0;end
    else begin fill_bcd<=shifted[11:0];busy<=0;end
   end
  end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Isolated layout: 8 x 40px traces above; servo table left and 24-frame whale right; one common background.
module b_v2_renderer(
 input wire clk,rst_n,input wire sample_valid,input wire [127:0] sample_data,
 input wire [319:0] servo_snapshot,
 input wire history_clear,freeze,input wire [2:0] page,
 input wire [15:0] gain,sample_rate,class_id,generation,mapped_action,mapped_pose,
 input wire [127:0] channel_gains,ranges,input wire [7:0] custom,
 input wire armed,saturated,link_stale,
 output reg [7:0] vga_r,vga_g,vga_b,output reg vga_hs,vga_vs,
 output wire vga_clk,frame_start,output reg [31:0] sample_count,frame_count,
 output reg wave_valid,output wire [9:0] debug_x,output wire [8:0] debug_y,
 output wire debug_de,output wire [4:0] animation_frame
);
 wire hs,vs,de;wire [9:0] x;wire [11:0] h,v;wire [8:0] y;wire [8:0] wave_x;wire [4:0] wave_phase;
 vga_timing timing(.clk(clk),.rst_n(rst_n),.h(h),.v(v),.hs(hs),.vs(vs),
  .de(de),.x(x),.y(y),.frame_start(frame_start),.wave_x(wave_x),.wave_phase(wave_phase));
 assign vga_clk=~clk;
 reg [9:0] write_ptr,view_base,history;
 reg [127:0] sample_data1;reg sample_valid1;
 reg [127:0] centers;
 reg [21:0] mean_sum[0:7];
 reg [3:0] mean_state;
 reg [7:0] mean_pair;
 reg [2:0] mean_lane;
 reg [8:0] mean_even_addr,mean_odd_addr;
 reg [127:0] mean_even_word,mean_odd_word;
 reg [15:0] mean_even_lane,mean_odd_lane;
 reg [16:0] mean_pair_value;
 reg mean_bad,mean_fault;
 wire [127:0] mean_even_data,mean_odd_data;
 wire mean_busy=mean_state!=0;
 wire mean_read=mean_busy;
 wire [9:0] mean_base=write_ptr-10'd512;
 integer mean_i;
 // Read the existing 1024-point history only during vertical blanking.
 // Eight channels share one sum datapath. Every scan begins from zero;
 // no read/write collision or old differential sum can cause drift.
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   centers<=0;mean_state<=0;mean_pair<=0;mean_lane<=0;
   mean_even_addr<=0;mean_odd_addr<=0;mean_bad<=0;mean_fault<=0;
   for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
  end else if(history_clear)begin
   centers<=0;mean_state<=0;mean_bad<=0;mean_fault<=0;
   for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
  end else if(mean_busy && v>=41)begin
   mean_state<=0;mean_fault<=1;
  end else case(mean_state)
   0:if(view_commit && page==0 && history>=512 && !freeze && v<41)begin
    mean_even_addr<=mean_base[9:1]+mean_base[0];mean_odd_addr<=mean_base[9:1];
    mean_pair<=0;mean_lane<=0;mean_bad<=0;mean_state<=1;
    for(mean_i=0;mean_i<8;mean_i=mean_i+1)mean_sum[mean_i]<=0;
   end
   1:mean_state<=2;
   2:mean_state<=3;
   3:begin mean_even_word<=mean_even_data;mean_odd_word<=mean_odd_data;mean_lane<=0;mean_state<=4;end
   4:begin
    mean_even_lane<=mean_even_word[mean_lane*16+:16];mean_odd_lane<=mean_odd_word[mean_lane*16+:16];mean_state<=5;
   end
   5:begin
    mean_pair_value<={1'b0,mean_even_lane}+{1'b0,mean_odd_lane};
    if(mean_even_lane>3300 || mean_odd_lane>3300)mean_bad<=1;
    mean_state<=6;
   end
   6:begin
    mean_sum[mean_lane]<=mean_sum[mean_lane]+mean_pair_value;
    if(mean_lane!=7)begin mean_lane<=mean_lane+1'b1;mean_state<=4;end
    else if(mean_pair==255)mean_state<=7;
    else begin
     mean_pair<=mean_pair+1'b1;mean_even_addr<=mean_even_addr+1'b1;mean_odd_addr<=mean_odd_addr+1'b1;mean_state<=1;
    end
   end
   7:begin
    if(!mean_bad)for(mean_i=0;mean_i<8;mean_i=mean_i+1)centers[mean_i*16+:16]<=mean_sum[mean_i]>>9;
    else mean_fault<=1;
    mean_state<=0;
   end
   default:begin mean_state<=0;mean_fault<=1;end
  endcase
 end
 reg view_waiting;
 wire view_commit=(frame_start || view_waiting) && !sample_valid1 && !mean_busy;
 always @(posedge clk or negedge rst_n)if(!rst_n)view_waiting<=0;else if(history_clear)view_waiting<=0;else if(view_commit)view_waiting<=0;else if(frame_start)view_waiting<=1;

 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin sample_valid1<=0;end
  else begin sample_valid1<=sample_valid && !freeze && !history_clear && page<4;end
 always @(posedge clk)sample_data1<=sample_data;
 wire [23:0] native_rgb;wire native_active,native_clip;
 pcs_native_trace native_trace(.clk(clk),.rst_n(rst_n),.sample_valid(sample_valid1 && !history_clear),
  .sample_data(sample_data1),.raw_page(page==0),.mean_read(mean_read),.mean_even_addr(mean_even_addr),.mean_odd_addr(mean_odd_addr),.mean_even_data(mean_even_data),.mean_odd_data(mean_odd_data),.centers(centers),.ranges(ranges),.gains(channel_gains),.custom(custom),.write_ptr(write_ptr),.view_base(view_base),.wave_valid(wave_valid),.view_commit(view_commit),.history_clear(history_clear),.clipped(native_clip),
  .h(h),.v(v),.sample_index(wave_x),.sample_phase(wave_phase),.rgb(native_rgb),.active(native_active));
 // Coordinate stage ends the raster subtract/decode before display addressing.
 reg [9:0] x0;reg [8:0] y0,wave_x0;reg hs0,vs0,de0;
 always @(posedge clk)begin x0<=x;y0<=y;wave_x0<=wave_x;hs0<=hs;vs0<=vs;de0<=de;end
 b_v2_whale_player whale_player(.clk(clk),.rst_n(rst_n),.frame_start(frame_start),.frame(animation_frame));
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin write_ptr<=0;view_base<=0;history<=0;sample_count<=0;frame_count<=0;
   wave_valid<=0;end
  else begin
   if(sample_valid1)begin write_ptr<=write_ptr+1'b1;sample_count<=sample_count+1'b1;
    if(history<512)history<=history+1'b1;end
   if(history_clear)begin write_ptr<=0;view_base<=0;history<=0;wave_valid<=0;end
   if(view_commit && !history_clear)begin
    frame_count<=frame_count+1'b1;
    if(history>=512)begin view_base<=write_ptr-10'd512;wave_valid<=1;end
   end
  end
 wire [23:0] sprite_rgb;
 // 64 stored pixels -> 112 displayed pixels. Scaling changes addresses, not ROM depth.
 // Right-hand 256x112 region: center the largest square without shrinking traces.
 reg [6:0] sprite_dx,sprite_dy;
 always @(posedge clk)begin sprite_dx<=x-10'd456;sprite_dy<=y-9'd368;end
 function [5:0] sprite_scale;input [6:0] d;
  begin case(d)
   7'd0:sprite_scale=6'd0;
   7'd1:sprite_scale=6'd0;
   7'd2:sprite_scale=6'd1;
   7'd3:sprite_scale=6'd1;
   7'd4:sprite_scale=6'd2;
   7'd5:sprite_scale=6'd2;
   7'd6:sprite_scale=6'd3;
   7'd7:sprite_scale=6'd4;
   7'd8:sprite_scale=6'd4;
   7'd9:sprite_scale=6'd5;
   7'd10:sprite_scale=6'd5;
   7'd11:sprite_scale=6'd6;
   7'd12:sprite_scale=6'd6;
   7'd13:sprite_scale=6'd7;
   7'd14:sprite_scale=6'd8;
   7'd15:sprite_scale=6'd8;
   7'd16:sprite_scale=6'd9;
   7'd17:sprite_scale=6'd9;
   7'd18:sprite_scale=6'd10;
   7'd19:sprite_scale=6'd10;
   7'd20:sprite_scale=6'd11;
   7'd21:sprite_scale=6'd12;
   7'd22:sprite_scale=6'd12;
   7'd23:sprite_scale=6'd13;
   7'd24:sprite_scale=6'd13;
   7'd25:sprite_scale=6'd14;
   7'd26:sprite_scale=6'd14;
   7'd27:sprite_scale=6'd15;
   7'd28:sprite_scale=6'd16;
   7'd29:sprite_scale=6'd16;
   7'd30:sprite_scale=6'd17;
   7'd31:sprite_scale=6'd17;
   7'd32:sprite_scale=6'd18;
   7'd33:sprite_scale=6'd18;
   7'd34:sprite_scale=6'd19;
   7'd35:sprite_scale=6'd20;
   7'd36:sprite_scale=6'd20;
   7'd37:sprite_scale=6'd21;
   7'd38:sprite_scale=6'd21;
   7'd39:sprite_scale=6'd22;
   7'd40:sprite_scale=6'd22;
   7'd41:sprite_scale=6'd23;
   7'd42:sprite_scale=6'd24;
   7'd43:sprite_scale=6'd24;
   7'd44:sprite_scale=6'd25;
   7'd45:sprite_scale=6'd25;
   7'd46:sprite_scale=6'd26;
   7'd47:sprite_scale=6'd26;
   7'd48:sprite_scale=6'd27;
   7'd49:sprite_scale=6'd28;
   7'd50:sprite_scale=6'd28;
   7'd51:sprite_scale=6'd29;
   7'd52:sprite_scale=6'd29;
   7'd53:sprite_scale=6'd30;
   7'd54:sprite_scale=6'd30;
   7'd55:sprite_scale=6'd31;
   7'd56:sprite_scale=6'd32;
   7'd57:sprite_scale=6'd32;
   7'd58:sprite_scale=6'd33;
   7'd59:sprite_scale=6'd33;
   7'd60:sprite_scale=6'd34;
   7'd61:sprite_scale=6'd34;
   7'd62:sprite_scale=6'd35;
   7'd63:sprite_scale=6'd36;
   7'd64:sprite_scale=6'd36;
   7'd65:sprite_scale=6'd37;
   7'd66:sprite_scale=6'd37;
   7'd67:sprite_scale=6'd38;
   7'd68:sprite_scale=6'd38;
   7'd69:sprite_scale=6'd39;
   7'd70:sprite_scale=6'd40;
   7'd71:sprite_scale=6'd40;
   7'd72:sprite_scale=6'd41;
   7'd73:sprite_scale=6'd41;
   7'd74:sprite_scale=6'd42;
   7'd75:sprite_scale=6'd42;
   7'd76:sprite_scale=6'd43;
   7'd77:sprite_scale=6'd44;
   7'd78:sprite_scale=6'd44;
   7'd79:sprite_scale=6'd45;
   7'd80:sprite_scale=6'd45;
   7'd81:sprite_scale=6'd46;
   7'd82:sprite_scale=6'd46;
   7'd83:sprite_scale=6'd47;
   7'd84:sprite_scale=6'd48;
   7'd85:sprite_scale=6'd48;
   7'd86:sprite_scale=6'd49;
   7'd87:sprite_scale=6'd49;
   7'd88:sprite_scale=6'd50;
   7'd89:sprite_scale=6'd50;
   7'd90:sprite_scale=6'd51;
   7'd91:sprite_scale=6'd52;
   7'd92:sprite_scale=6'd52;
   7'd93:sprite_scale=6'd53;
   7'd94:sprite_scale=6'd53;
   7'd95:sprite_scale=6'd54;
   7'd96:sprite_scale=6'd54;
   7'd97:sprite_scale=6'd55;
   7'd98:sprite_scale=6'd56;
   7'd99:sprite_scale=6'd56;
   7'd100:sprite_scale=6'd57;
   7'd101:sprite_scale=6'd57;
   7'd102:sprite_scale=6'd58;
   7'd103:sprite_scale=6'd58;
   7'd104:sprite_scale=6'd59;
   7'd105:sprite_scale=6'd60;
   7'd106:sprite_scale=6'd60;
   7'd107:sprite_scale=6'd61;
   7'd108:sprite_scale=6'd61;
   7'd109:sprite_scale=6'd62;
   7'd110:sprite_scale=6'd62;
   7'd111:sprite_scale=6'd63;
   7'd112:sprite_scale=6'd0;
   7'd113:sprite_scale=6'd0;
   7'd114:sprite_scale=6'd1;
   7'd115:sprite_scale=6'd1;
   7'd116:sprite_scale=6'd2;
   7'd117:sprite_scale=6'd2;
   7'd118:sprite_scale=6'd3;
   7'd119:sprite_scale=6'd4;
   7'd120:sprite_scale=6'd4;
   7'd121:sprite_scale=6'd5;
   7'd122:sprite_scale=6'd5;
   7'd123:sprite_scale=6'd6;
   7'd124:sprite_scale=6'd6;
   7'd125:sprite_scale=6'd7;
   7'd126:sprite_scale=6'd8;
   7'd127:sprite_scale=6'd8;
   default:sprite_scale=0;
  endcase end
 endfunction
 reg [5:0] sprite_x_scaled,sprite_y_scaled;
 always @(posedge clk)begin sprite_x_scaled<=sprite_scale(sprite_dx);sprite_y_scaled<=sprite_scale(sprite_dy);end
 b_v2_whale_sprite sprite(.clk(clk),.frame(animation_frame),.x(sprite_x_scaled[5:0]),.y(sprite_y_scaled[5:0]),.rgb(sprite_rgb));
 reg [9:0] x1;reg [8:0] y1,wave_x1,wave_x2;reg hs1,vs1,de1;
 always @(posedge clk)begin x1<=x0;y1<=y0;wave_x1<=wave_x0;hs1<=hs0;vs1<=vs0;de1<=de0;end
 // UI observes the samples arriving in the pixel domain, including while frozen.
 reg wave_arrived,wave_seen;reg [5:0] idle_frames;reg flow_active;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin wave_arrived<=0;wave_seen<=0;idle_frames<=30;flow_active<=0;end
  else begin
   if(sample_valid)begin wave_arrived<=1;wave_seen<=1;end
   if(frame_start)begin
    wave_arrived<=sample_valid;
    if(wave_arrived || sample_valid)begin idle_frames<=0;flow_active<=1;end
    else if(idle_frames<30)begin idle_frames<=idle_frames+1'b1;if(idle_frames==29)flow_active<=0;end
   end
  end
 reg number_update;
 always @(posedge clk or negedge rst_n)if(!rst_n)number_update<=0;else number_update<=view_commit || mean_state==7;
 reg [12:0] number_write;
 reg [15:0] number_result;
 (* preserve="true" *) reg [15:0] number_result_center,number_result_position;
 reg [127:0] centers_bcd;
 wire [127:0] ranges_bcd;
 reg [79:0] positions_bcd;
 (* preserve="true" *) reg [12:0] number_slot;
 (* preserve="true" *) reg [18:0] number_phase;
 reg numbers_busy;
 reg [31:0] number_shift;
 reg [31:0] number_adjusted;
 reg [15:0] number_selected;
 integer number_k,number_source;
 always @*begin
  number_selected=0;
  for(number_source=0;number_source<8;number_source=number_source+1)begin
   number_selected=number_selected | ({16{number_slot[number_source]}} & centers[number_source*16+:16]);
  end
  for(number_source=0;number_source<5;number_source=number_source+1)begin
   number_selected=number_selected | ({16{number_slot[8+number_source]}} & servo_snapshot[16+number_source*16+:16]);
  end
  number_adjusted=number_shift;
  for(number_k=16;number_k<32;number_k=number_k+4)
   if(number_shift[number_k+:4]>=5)number_adjusted[number_k+:4]=number_shift[number_k+:4]+3;
 end
 reg [15:0] number_input;
 always @(posedge clk)number_input<=number_selected;
 integer number_destination;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   number_slot<=1;number_phase<=1;numbers_busy<=0;
   number_shift<=0;number_write<=0;number_result<=0;number_result_center<=0;number_result_position<=0;
  end else begin
   for(number_destination=0;number_destination<13;number_destination=number_destination+1)
    number_write[number_destination]<=numbers_busy && number_phase[18] && number_slot[number_destination];
   number_result<=number_shift[31:16];number_result_center<=number_shift[31:16];number_result_position<=number_shift[31:16];
   if(number_update)begin number_slot<=1;number_phase<=1;numbers_busy<=1;end
   else if(numbers_busy)begin
    number_phase<={number_phase[17:0],number_phase[18]};
    if(number_phase[1])number_shift<={16'd0,number_input};
    else if(|number_phase[17:2])number_shift<=number_adjusted<<1;
    if(number_phase[18])begin
     number_slot<={number_slot[11:0],1'b0};
     if(number_slot[12])numbers_busy<=0;
    end
   end
  end
 end
 genvar bcd_item;
 generate for(bcd_item=0;bcd_item<13;bcd_item=bcd_item+1)begin:bcd_destination
  if(bcd_item<8)begin
   always @(posedge clk or negedge rst_n)if(!rst_n)centers_bcd[bcd_item*16+:16]<=0;else if(number_write[bcd_item])centers_bcd[bcd_item*16+:16]<=number_result_center;
  end else begin
   always @(posedge clk or negedge rst_n)if(!rst_n)positions_bcd[(bcd_item-8)*16+:16]<=0;else if(number_write[bcd_item])positions_bcd[(bcd_item-8)*16+:16]<=number_result_position;
  end
 end endgenerate
 // Seven legal ranges have fixed decimal digits; no runtime conversion.
 function [15:0] range_decimal;input [15:0] mv;begin case(mv)
  20:range_decimal=16'h0020;50:range_decimal=16'h0050;
  100:range_decimal=16'h0100;200:range_decimal=16'h0200;
  500:range_decimal=16'h0500;1200:range_decimal=16'h1200;
  3300:range_decimal=16'h3300;default:range_decimal=16'hffff;
 endcase end endfunction
 generate for(bcd_item=0;bcd_item<8;bcd_item=bcd_item+1)begin:range_lookup
  assign ranges_bcd[bcd_item*16+:16]=range_decimal(ranges[bcd_item*16+:16]);
 end endgenerate
 function [6:0] hexchar;input [3:0] n;begin hexchar=n<10?48+n:65+n-10;end endfunction
 function [2:0] lane_id;input [8:0] yy;begin
  if(yy<88)lane_id=0;else if(yy<128)lane_id=1;else if(yy<168)lane_id=2;
  else if(yy<208)lane_id=3;else if(yy<248)lane_id=4;else if(yy<288)lane_id=5;
  else if(yy<328)lane_id=6;else lane_id=7;
 end endfunction
 function [5:0] lane_offset;input [8:0] yy;begin
  case(lane_id(yy))
   0:lane_offset=yy-48;1:lane_offset=yy-88;2:lane_offset=yy-128;3:lane_offset=yy-168;
   4:lane_offset=yy-208;5:lane_offset=yy-248;6:lane_offset=yy-288;7:lane_offset=yy-328;
  endcase
 end endfunction
 wire [6:0] col0=x0[9:3],col=x1[9:3];
 reg [2:0] channel_early;reg [5:0] lane_early;
 // Prepare only range, RAW mean and finger position digits.
 reg [31:0] range_digits,center_digits;reg [19:0] position_digits;
 integer digit_lane;
 wire [2:0] servo_row_input=y[6:4],servo_row=y0[6:4];
 reg servo_online0,servo_torque0,servo_feedback0,servo_torque_valid0;
 reg custom0;
 always @(posedge clk)begin
  channel_early<=lane_id(y);lane_early<=lane_offset(y);
  custom0<=custom[lane_id(y)];
  for(digit_lane=0;digit_lane<8;digit_lane=digit_lane+1)begin
   range_digits[digit_lane*4+:4]<=ranges_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
   center_digits[digit_lane*4+:4]<=centers_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
  end
  for(digit_lane=0;digit_lane<5;digit_lane=digit_lane+1)
   position_digits[digit_lane*4+:4]<=positions_bcd[digit_lane*16+{~x[4:3],2'b00}+:4];
  servo_online0<=servo_snapshot[256+servo_row_input];
  servo_torque0<=servo_snapshot[261+servo_row_input];
  servo_feedback0<=servo_snapshot[266+servo_row_input];
  servo_torque_valid0<=servo_snapshot[289+servo_row_input];
 end
 reg [4:0] text_region;
 reg [5:0] text_column;
 always @(posedge clk)begin
  text_region[0]<=y<16;
  text_region[1]<=y>=16 && y<32;
  text_region[2]<=y>=48 && y<368 && lane_offset(y)<16 && x<64;
  text_region[3]<=y>=48 && y<368 && lane_offset(y)>=16 && lane_offset(y)<32 && x<64;
  text_region[4]<=y>=384 && y<464 && x<384;
  text_column[0]<=x[9:3]==1;
  text_column[1]<=x[9:3]>=4 && x[9:3]<8;
  text_column[2]<=x[9:3]==2;
  text_column[3]<=x[9:3]>=8 && x[9:3]<12;
  text_column[4]<=x[9:3]==17;
  text_column[5]<=x[9:3]==23;
 end
 reg [2:0] template_id,template_id_early;
 reg text_enabled,text_enabled_early,text_enabled2;
 reg [23:0] text_colour,text_colour_early;
 reg dynamic_valid,dynamic_valid_early;
 reg [6:0] dynamic_terms[0:5];
 wire [6:0] dynamic_character=dynamic_terms[0]|dynamic_terms[1]|dynamic_terms[2]|dynamic_terms[3]|dynamic_terms[4]|dynamic_terms[5];
 wire range_active=text_region[2] && text_column[1];
 wire center_active=text_region[3] && page==0 && text_column[1];
 wire position_active=text_region[4] && text_column[3];
 wire channel_active=text_region[2] && text_column[0];
 wire finger_active=text_region[4] && text_column[2];
 wire status_active=text_region[1] && (col0==5 || col0==14 || col0==23 || col0==30);
 wire finger_flags=text_region[4] && (text_column[4] || text_column[5]);
 wire [3:0] range_nibble=range_digits[channel_early*4+:4];
 wire [6:0] custom_character=col0==4?7'd67:col0==5?7'd85:col0==6?7'd83:7'd84;
 wire [6:0] range_character=custom0?custom_character:hexchar(range_nibble);
 wire [6:0] center_character=hexchar(center_digits[channel_early*4+:4]);
 wire [6:0] position_character=servo_feedback0?hexchar(position_digits[servo_row*4+:4]):7'd63;
 reg [6:0] status_character;
 always @*begin
  status_character=7'd48;
  case(col0)
   5:status_character=freeze?7'd49:7'd48;
   14:status_character=(!link_stale && flow_active)?7'd49:7'd48;
   23:status_character=(saturated || native_clip)?7'd49:7'd48;
   30:status_character=armed?7'd49:7'd48;
  endcase
 end
 wire [6:0] finger_character=text_column[4]?(servo_online0?7'd49:7'd48):
  !servo_torque_valid0?7'd63:servo_torque0?7'd49:7'd48;
 always @*begin
  template_id_early=({3{text_region[0]}} & page[1:0]) |
   ({3{text_region[1]}} & 3'd4) | ({3{text_region[2]}} & 3'd5) |
   ({3{text_region[3]}} & 3'd6) | ({3{text_region[4]}} & 3'd7);
  text_enabled_early=text_region[0] || text_region[1] || text_region[2] ||
   (text_region[3] && page==0) || text_region[4];
  text_colour_early=text_region[2]?24'h65d9ef:24'h9cacc6;
  dynamic_valid_early=range_active || center_active || position_active || channel_active || finger_active || status_active || finger_flags;
 end
 always @(posedge clk)begin
  template_id<=template_id_early;text_enabled<=text_enabled_early;
  text_colour<=text_colour_early;dynamic_valid<=dynamic_valid_early;
  dynamic_terms[0]<=range_active?range_character:7'd0;
  dynamic_terms[1]<=center_active?center_character:7'd0;
  dynamic_terms[2]<=position_active?position_character:7'd0;
  dynamic_terms[3]<=channel_active?(7'd48+channel_early):finger_active?(7'd49+servo_row):7'd0;
  dynamic_terms[4]<=status_active?status_character:7'd0;
  dynamic_terms[5]<=finger_flags?finger_character:7'd0;
 end
 (* ram_style="block" *) reg [6:0] text_rows[0:1023];
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/gesture_tasks/assets/text_rows_minimal.hex",text_rows);
 wire [2:0] font_row=(y1>=48 && y1<368 && col<8)?lane_offset(y1)>>1:y1[3:1];
 reg [6:0] static_character2,character3;reg dynamic_valid2;reg [6:0] dynamic_character2;
 reg [2:0] font_row2,font_row3,font_col2,font_col3,font_col4,font_col5;
 reg [23:0] base_rgb2,text_colour2,base_rgb3,text_colour3,base_rgb4,text_colour4,base_rgb5,text_colour5;
 reg hs2,vs2,de_mid,hs3,vs3,de3,hs4,vs4,de4,hs5,vs5,de5;
 reg [9:0] x_mid,x3,x4,x5;reg [8:0] y_mid,y3,y4,y5;
 always @(posedge clk)static_character2<=text_rows[{template_id,col}];
 wire [4:0] font_row_bits;reg [4:0] font_bits5;
 pcs_font_row font(.clk(clk),.character(character3),.row(font_row3),.row_bits(font_row_bits));
 wire font_dot=(font_col5<5)?font_bits5[4-font_col5]:1'b0;
 reg de6,hs6,vs6;
 reg [9:0] x6;reg [8:0] y6;
 reg [23:0] overlay_rgb6,native_rgb6;reg native_active6;
 always @(posedge clk)begin
  overlay_rgb6<=font_dot?text_colour5:base_rgb5;
  de6<=de5;hs6<=hs5;vs6<=vs5;x6<=x5;y6<=y5;
  native_rgb6<=native_rgb;native_active6<=native_active;
 end
 // Sprite: delta -> scale -> pixel BRAM -> LUT palette -> late composition.
 reg sprite_active2,sprite_active3,sprite_active4;
 always @(posedge clk)begin
  sprite_active2<=x1>=456 && x1<568 && y1>=368 && y1<480;
  sprite_active3<=sprite_active2;sprite_active4<=sprite_active3;
 end
 wire [23:0] rgb=24'h080c18;
 always @(posedge clk)begin dynamic_valid2<=dynamic_valid;text_enabled2<=text_enabled;dynamic_character2<=dynamic_character;font_row2<=font_row;font_col2<=x1[2:0];base_rgb2<=rgb;
   text_colour2<=text_colour;hs2<=hs1;vs2<=vs1;de_mid<=de1;x_mid<=x1;y_mid<=y1;end
 always @(posedge clk)begin character3<=!text_enabled2?7'd32:dynamic_valid2?dynamic_character2:static_character2;font_row3<=font_row2;font_col3<=font_col2;
   base_rgb3<=0;text_colour3<=text_colour2;hs3<=hs2;vs3<=vs2;de3<=de_mid;x3<=x_mid;y3<=y_mid;end
 always @(posedge clk)begin font_col4<=font_col3;base_rgb4<=rgb;text_colour4<=text_colour3;
   hs4<=hs3;vs4<=vs3;de4<=de3;x4<=x3;y4<=y3;end
 always @(posedge clk)begin font_bits5<=font_row_bits;font_col5<=font_col4;base_rgb5<=sprite_active4?sprite_rgb:base_rgb4;text_colour5<=text_colour4;
   hs5<=hs4;vs5<=vs4;de5<=de4;x5<=x4;y5<=y4;end
 reg [9:0] x2;reg [8:0] y2;reg de2;
 reg [7:0] overlay_r,overlay_g,overlay_b;reg overlay_hs,overlay_vs;
 reg [45:0] overlay_pipe[0:14];integer pipe_i;
 reg [9:0] final_x;reg [8:0] final_y;reg final_de;
 assign debug_x=final_x;assign debug_y=final_y;assign debug_de=final_de;
 // Local reset bank keeps the wide pixel reset tree away from final IOs.
 (* preserve="true",async_reg="true" *) reg output_reset_n;
 reg [23:0] output_flush;
 always @(posedge clk or negedge rst_n)
  if(!rst_n)begin output_reset_n<=0;output_flush<=0;end else begin output_flush<={output_flush[22:0],1'b1};output_reset_n<=output_flush[23];end
 // Final composite register permits RGB/HS/VS packing into the original IOs.
 always @(posedge clk or negedge output_reset_n)
  if(!output_reset_n)begin vga_r<=0;vga_g<=0;vga_b<=0;vga_hs<=0;vga_vs<=0;final_x<=0;final_y<=0;final_de<=0;end
  else begin
   {vga_hs,vga_vs,final_de,final_x,final_y}<=overlay_pipe[14][45:24];
   {vga_r,vga_g,vga_b}<=overlay_pipe[14][43] ? ((native_active6 && page<4)?native_rgb6:overlay_pipe[14][23:0]) : 24'd0;
  end
 always @(posedge clk)begin
   overlay_pipe[0]<={overlay_hs,overlay_vs,de2,x2,y2,overlay_r,overlay_g,overlay_b};
   for(pipe_i=1;pipe_i<15;pipe_i=pipe_i+1)overlay_pipe[pipe_i]<=overlay_pipe[pipe_i-1];
  end
 always @(posedge clk)begin {overlay_r,overlay_g,overlay_b}<=de6?overlay_rgb6:24'd0;
   overlay_hs<=hs6;overlay_vs<=vs6;x2<=x6;y2<=y6;de2<=de6;end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Bundled-data CDC: hold is immutable from request until returned acknowledge.
// Destination publishes two pixel cycles after the frame boundary, in blanking.
// Local 32-bit reset/capture banks keep wide payload fanout off control paths.
module b_v2_mailbox #(parameter WIDTH=32)(
 input wire src_clk, src_rst_n, input wire src_valid,
 output wire src_ready, input wire [WIDTH-1:0] src_data,
 input wire dst_clk, dst_rst_n, dst_commit,
 output reg [WIDTH-1:0] dst_data, output reg dst_pulse);
 reg [WIDTH-1:0] payload_hold;
 reg req, ack;
 reg commit_q;
 always @(posedge dst_clk or negedge dst_rst_n)
  if(!dst_rst_n)commit_q<=0;else commit_q<=dst_commit;

 (* preserve="true", async_reg="true" *) reg ack_s1,ack_s2,req_s1,req_s2;
 assign src_ready=(req==ack_s2);
 always @(posedge src_clk or negedge src_rst_n)
  if(!src_rst_n) begin req<=0; payload_hold<=0; ack_s1<=0;ack_s2<=0; end
  else begin
   ack_s1<=ack; ack_s2<=ack_s1;
   if(src_valid && src_ready) begin payload_hold<=src_data; req<=~req; end
  end
 reg capture_pending;
 always @(posedge dst_clk or negedge dst_rst_n)
  if(!dst_rst_n) begin req_s1<=0;req_s2<=0;ack<=0;capture_pending<=0;dst_pulse<=0; end
  else begin
   req_s1<=req;req_s2<=req_s1;
   capture_pending<=commit_q && req_s2!=ack;dst_pulse<=capture_pending;
   if(commit_q && req_s2!=ack) begin
    ack<=req_s2;
   end
  end
 genvar bank;
 generate for(bank=0;bank<(WIDTH+31)/32;bank=bank+1)begin: payload_bank
  localparam BITS=(WIDTH-bank*32>=32)?32:WIDTH-bank*32;
  (* preserve="true" *) reg bank_reset_n,bank_capture;
  always @(posedge dst_clk or negedge dst_rst_n)
   if(!dst_rst_n)bank_reset_n<=0;else bank_reset_n<=1;
  always @(posedge dst_clk or negedge bank_reset_n)
   if(!bank_reset_n)begin bank_capture<=0;dst_data[bank*32+:BITS]<=0;end
   else begin
    bank_capture<=commit_q && req_s2!=ack;
    if(bank_capture)dst_data[bank*32+:BITS]<=payload_hold[bank*32+:BITS];
   end
 end endgenerate
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// Binary request collector and backpressured framed response transmitter.
module b_v2_pc(
 input wire clk,rst_n,frame_valid,output wire frame_ready,input wire [7:0] frame_type,
 input wire [10:0] frame_length,input wire payload_valid,output wire payload_ready,
 input wire [7:0] payload_data,input wire payload_last,
 output reg command_valid,input wire command_ready,
 output reg [15:0] request_id,opcode,data_length,output reg [79:0] arguments,
 input wire response_valid,output wire response_ready,input wire [7:0] response_type,
 input wire [15:0] response_id,response_opcode,response_status,
 input wire [9:0] response_length,input wire [4095:0] response_data,input wire [7:0] response_byte,output wire [9:0] response_byte_index,
 output wire uart_tx,output reg [31:0] rejects
);
 reg collecting;reg [10:0] index,length;reg [7:0] kind;reg [143:0] packet;
 wire [143:0] complete={payload_data,packet[143:8]};
 // Assemble indexed bytes, so malformed long packets cannot shift a valid suffix into a request.
 reg [143:0] fixed_packet;
 reg evaluate;
 assign frame_ready=!collecting && !command_valid && !evaluate;
 assign payload_ready=collecting;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin collecting<=0;index<=0;length<=0;kind<=0;packet<=0;fixed_packet<=0;
   command_valid<=0;request_id<=0;opcode<=0;data_length<=0;arguments<=0;rejects<=0;evaluate<=0;end
  else begin
   if(command_valid && command_ready)command_valid<=0;
   if(frame_valid && frame_ready)begin
    collecting<=frame_length!=0;index<=0;length<=frame_length;kind<=frame_type;fixed_packet<=0;
    if(frame_length==0)rejects<=rejects+1'b1;
   end
   if(payload_valid && payload_ready)begin
    if(index<18)fixed_packet[index*8+:8]<=payload_data;
    index<=index+1'b1;
    if(payload_last)begin collecting<=0;evaluate<=1;end
   end
   if(evaluate)begin
    evaluate<=0;
    if((kind==8'h50 || kind==8'h42) && length>=8 && length<=18 && index==length && fixed_packet[7:0]==1 &&
     fixed_packet[15:8]==2 && fixed_packet[63:48]==length-8)begin
     request_id<=fixed_packet[31:16];opcode<=fixed_packet[47:32];data_length<=fixed_packet[63:48];
     arguments<=fixed_packet[143:64];command_valid<=1;
    end else rejects<=rejects+1'b1;
   end
  end
 end
 reg sending,buffering,copy_phase;
 assign response_byte_index=copy_index;reg [9:0] copy_index;reg [10:0] tx_index,total;reg [15:0] tx_sequence,crc;
 reg [7:0] kind_hold;reg [15:0] id_hold,op_hold,status_hold;reg [9:0] len_hold;
 (* ram_style="block" *) reg [7:0] data_buffer[0:511];reg [7:0] buffer_byte;
 // The response producer holds its snapshot until the byte copy completes.
 // UART transmission then owns a compact BRAM, freeing PC/control for STOP.
 always @(posedge clk)begin
  if(buffering && copy_phase && copy_index<len_hold)data_buffer[copy_index[8:0]]<=
   response_byte;
  buffer_byte<=data_buffer[tx_index[8:0]-9'd22];
 end
 reg tx_start;wire tx_busy;reg [7:0] tx_data;reg [7:0] next_byte;
 wire [15:0] crc_next;
 link_crc16_ccitt crc_step(.crc_in(crc),.data_in(next_byte),.crc_out(crc_next));
 wire [10:0] outer_len={1'b0,len_hold}+11'd10;
 assign response_ready=buffering && copy_index==len_hold;
 always @*begin
  next_byte=0;
  case(tx_index)
   0:next_byte=8'ha5;1:next_byte=8'h5a;2:next_byte=1;3:next_byte=kind_hold;
   4:next_byte=tx_sequence[7:0];5:next_byte=tx_sequence[15:8];
   6:next_byte=outer_len[7:0];7:next_byte={5'd0,outer_len[10:8]};
   12:next_byte=1;13:next_byte=2;14:next_byte=id_hold[7:0];15:next_byte=id_hold[15:8];
   16:next_byte=op_hold[7:0];17:next_byte=op_hold[15:8];
   18:next_byte=status_hold[7:0];19:next_byte=status_hold[15:8];
   20:next_byte=len_hold[7:0];21:next_byte={6'd0,len_hold[9:8]};
   default:begin
    if(tx_index>=22 && tx_index<22+len_hold)next_byte=buffer_byte;
    if(tx_index==total-2)next_byte=crc[7:0];
    if(tx_index==total-1)next_byte=crc[15:8];
   end
  endcase
 end
 uart_tx_byte #(.CLKS_PER_BIT(26)) tx(.clk(clk),.rst_n(rst_n),.start(tx_start),.data(tx_data),.tx(uart_tx),.busy(tx_busy));
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin sending<=0;buffering<=0;copy_phase<=0;copy_index<=0;tx_index<=0;total<=0;tx_sequence<=0;crc<=16'hffff;kind_hold<=0;
   id_hold<=0;op_hold<=0;status_hold<=0;len_hold<=0;tx_start<=0;tx_data<=0;end
  else begin
   tx_start<=0;
   if(response_valid && !sending && !buffering && !tx_busy && !tx_start)begin
    buffering<=1;copy_phase<=0;copy_index<=0;tx_index<=0;total<=24+response_length;crc<=16'hffff;
    kind_hold<=response_type;id_hold<=response_id;op_hold<=response_opcode;status_hold<=response_status;
    len_hold<=response_length;
   end
   if(buffering)begin
    copy_phase<=!copy_phase;
    if(copy_index==len_hold)begin buffering<=0;sending<=1;end
    else if(copy_phase)copy_index<=copy_index+1'b1;
   end
   if(sending && !tx_busy && !tx_start)begin
    tx_data<=next_byte;tx_start<=1;
    if(tx_index>=2 && tx_index<total-2)crc<=crc_next;
    if(tx_index==total-1)begin sending<=0;tx_sequence<=tx_sequence+1'b1;end
    else tx_index<=tx_index+1'b1;
   end
  end
 end
endmodule
`default_nettype wire

`timescale 1ns/1ps

module uart_rx_byte #(
    parameter integer CLKS_PER_BIT = 24
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       enable,
    input  wire       rx,
    output reg  [7:0] data,
    output reg        valid,
    output reg        busy,
    output reg        framing_error
);

    localparam [1:0] RX_IDLE  = 2'd0;
    localparam [1:0] RX_START = 2'd1;
    localparam [1:0] RX_DATA  = 2'd2;
    localparam [1:0] RX_STOP  = 2'd3;

    reg [1:0] state;
    reg [15:0] clock_count;
    reg [2:0] bit_index;
    reg rx_meta;
    reg rx_sync;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            rx_meta <= 1'b1;
            rx_sync <= 1'b1;
        end else begin
            rx_meta <= rx;
            rx_sync <= rx_meta;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= RX_IDLE;
            clock_count   <= 16'd0;
            bit_index     <= 3'd0;
            data          <= 8'd0;
            valid         <= 1'b0;
            busy          <= 1'b0;
            framing_error <= 1'b0;
        end else begin
            valid         <= 1'b0;
            framing_error <= 1'b0;

            if (!enable) begin
                state       <= RX_IDLE;
                clock_count <= 16'd0;
                bit_index   <= 3'd0;
                busy        <= 1'b0;
            end else begin
                case (state)
                    RX_IDLE: begin
                        busy        <= 1'b0;
                        clock_count <= 16'd0;
                        bit_index   <= 3'd0;
                        if (!rx_sync) begin
                            state <= RX_START;
                            busy  <= 1'b1;
                        end
                    end

                    RX_START: begin
                        if (clock_count == (CLKS_PER_BIT / 2) - 1) begin
                            clock_count <= 16'd0;
                            if (!rx_sync)
                                state <= RX_DATA;
                            else begin
                                state <= RX_IDLE;
                                busy  <= 1'b0;
                            end
                        end else begin
                            clock_count <= clock_count + 1'b1;
                        end
                    end

                    RX_DATA: begin
                        if (clock_count == CLKS_PER_BIT - 1) begin
                            clock_count     <= 16'd0;
                            data[bit_index] <= rx_sync;
                            if (bit_index == 3'd7) begin
                                bit_index <= 3'd0;
                                state     <= RX_STOP;
                            end else begin
                                bit_index <= bit_index + 1'b1;
                            end
                        end else begin
                            clock_count <= clock_count + 1'b1;
                        end
                    end

                    RX_STOP: begin
                        if (clock_count == CLKS_PER_BIT - 1) begin
                            clock_count <= 16'd0;
                            state       <= RX_IDLE;
                            busy        <= 1'b0;
                            valid       <= rx_sync;
                            framing_error <= !rx_sync;
                        end else begin
                            clock_count <= clock_count + 1'b1;
                        end
                    end

                    default: state <= RX_IDLE;
                endcase
            end
        end
    end

endmodule

`timescale 1ns/1ps

module spi_flash_byte #(
    parameter integer HALF_CLKS = 12
) (
    input wire clk,
    input wire rst_n,
    input wire start,
    input wire [7:0] tx_data,
    input wire miso,
    output reg sclk,
    output reg mosi,
    output reg busy,
    output reg done,
    output reg [7:0] rx_data
);
    reg [7:0] tx_shift;
    reg [7:0] rx_shift;
    reg [2:0] bit_index;
    reg [7:0] half_count;
    reg high_phase;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sclk <= 1'b0;
            mosi <= 1'b1;
            busy <= 1'b0;
            done <= 1'b0;
            rx_data <= 8'd0;
            tx_shift <= 8'd0;
            rx_shift <= 8'd0;
            bit_index <= 3'd0;
            half_count <= 8'd0;
            high_phase <= 1'b0;
        end else begin
            done <= 1'b0;
            if (!busy) begin
                sclk <= 1'b0;
                if (start) begin
                    busy <= 1'b1;
                    tx_shift <= tx_data;
                    rx_shift <= 8'd0;
                    mosi <= tx_data[7];
                    bit_index <= 3'd0;
                    half_count <= 8'd0;
                    high_phase <= 1'b0;
                end
            end else if (half_count == HALF_CLKS - 1) begin
                half_count <= 8'd0;
                if (!high_phase) begin
                    sclk <= 1'b1;
                    rx_shift <= {rx_shift[6:0], miso};
                    high_phase <= 1'b1;
                end else begin
                    sclk <= 1'b0;
                    high_phase <= 1'b0;
                    if (bit_index == 3'd7) begin
                        busy <= 1'b0;
                        done <= 1'b1;
                        rx_data <= rx_shift;
                    end else begin
                        bit_index <= bit_index + 1'b1;
                        tx_shift <= {tx_shift[6:0], 1'b0};
                        mosi <= tx_shift[6];
                    end
                end
            end else half_count <= half_count + 1'b1;
        end
    end
endmodule

`timescale 1ns/1ps

module teach_flash_store (
    input wire clk,
    input wire rst_n,
    input wire save_req,
    input wire [3:0] save_group,
    input wire [49:0] save_positions,
    output reg init_done,
    output reg available,
    output reg load_valid,
    output reg [3:0] load_group,
    output reg [49:0] load_positions,
    output reg save_done,
    output reg save_ok,
    output reg flash_cs_n,
    output wire flash_clk,
    output wire flash_mosi,
    input wire flash_miso
);
    // TD 6.2.1 crashes when this module's state constants share one source
    // with the five packet engines; keep the Flash module separate in the project.
    // Transaction ops: ID=0, READ=1, WREN=2, ERASE=3, STATUS=4, PROGRAM=5.
    // States 0..8 cover startup/erase; 9..17 cover program/verify/SPI transfer.

    reg [4:0] state, return_state;
    reg [2:0] txn_op;
    reg [23:0] txn_addr;
    reg [4:0] txn_index;
    reg [1:0] gap_count;
    reg byte_start;
    reg [7:0] byte_tx;
    wire [7:0] byte_rx;
    wire byte_busy, byte_done;
    reg [7:0] id0, id1, id2, status_byte;
    reg [143:0] record_buf, write_record;
    reg [3:0] scan_group, pending_group;
    reg scan_slot, pending_slot;
    reg [7:0] pending_generation;
    reg candidate_valid, candidate_slot;
    reg [7:0] candidate_generation;
    reg [49:0] candidate_positions;
    reg [8:0] latest_valid;
    reg [8:0] latest_slot;
    reg [71:0] latest_generation;
    reg [15:0] poll_count;

    function [23:0] sector_address;
        input [3:0] group_index;
        input slot_index;
        begin
            sector_address = 24'h1e0000 + {7'd0, group_index, slot_index, 12'd0};
        end
    endfunction

    function [15:0] crc16;
        input [127:0] data;
        reg [15:0] crc;
        reg feedback;
        integer bit_number;
        begin
            crc = 16'hffff;
            for (bit_number = 0; bit_number < 128; bit_number = bit_number + 1) begin
                feedback = crc[15] ^ data[127 - bit_number];
                crc = {crc[14:0], 1'b0};
                if (feedback) crc = crc ^ 16'h1021;
            end
            crc16 = crc;
        end
    endfunction

    function [143:0] make_record;
        input [3:0] group_index;
        input [7:0] generation;
        input [49:0] positions;
        reg [127:0] body;
        begin
            body = {32'h53435335, 8'd1 + {4'd0, group_index}, generation,
                    6'd0, positions[9:0], 6'd0, positions[19:10],
                    6'd0, positions[29:20], 6'd0, positions[39:30],
                    6'd0, positions[49:40]};
            make_record = {body, crc16(body)};
        end
    endfunction

    function [49:0] decode_positions;
        input [79:0] raw;
        begin
            decode_positions = {raw[9:0], raw[25:16], raw[41:32],
                                raw[57:48], raw[73:64]};
        end
    endfunction

    wire valid_record = (record_buf[143:112] == 32'h53435335) &&
                        (record_buf[111:104] == {4'd0, scan_group} + 8'd1) &&
                        (record_buf[15:0] == crc16(record_buf[143:16]));
    wire newer_record = (record_buf[103:96] != candidate_generation) &&
                        ((record_buf[103:96] - candidate_generation) < 8'd128);

    spi_flash_byte byte_engine (
        .clk(clk), .rst_n(rst_n), .start(byte_start), .tx_data(byte_tx),
        .miso(flash_miso), .sclk(flash_clk), .mosi(flash_mosi),
        .busy(byte_busy), .done(byte_done), .rx_data(byte_rx)
    );

    always @(*) begin
        byte_tx = 8'd0;
        case (txn_op)
            3'd0: if (txn_index == 0) byte_tx = 8'h9f;
            3'd2: byte_tx = 8'h06;
            3'd4: if (txn_index == 0) byte_tx = 8'h05;
            3'd1, 3'd3, 3'd5: begin
                case (txn_index)
                    0: byte_tx = txn_op == 3'd1 ? 8'h03 :
                                 txn_op == 3'd3 ? 8'h20 : 8'h02;
                    1: byte_tx = txn_addr[23:16];
                    2: byte_tx = txn_addr[15:8];
                    3: byte_tx = txn_addr[7:0];
                    default: if (txn_op == 3'd5) begin
                        case (txn_index)
                            5'd4: byte_tx = write_record[143:136];
                            5'd5: byte_tx = write_record[135:128];
                            5'd6: byte_tx = write_record[127:120];
                            5'd7: byte_tx = write_record[119:112];
                            5'd8: byte_tx = write_record[111:104];
                            5'd9: byte_tx = write_record[103:96];
                            5'd10: byte_tx = write_record[95:88];
                            5'd11: byte_tx = write_record[87:80];
                            5'd12: byte_tx = write_record[79:72];
                            5'd13: byte_tx = write_record[71:64];
                            5'd14: byte_tx = write_record[63:56];
                            5'd15: byte_tx = write_record[55:48];
                            5'd16: byte_tx = write_record[47:40];
                            5'd17: byte_tx = write_record[39:32];
                            5'd18: byte_tx = write_record[31:24];
                            5'd19: byte_tx = write_record[23:16];
                            5'd20: byte_tx = write_record[15:8];
                            5'd21: byte_tx = write_record[7:0];
                            default: byte_tx = 8'd0;
                        endcase
                    end
                endcase
            end
            default: byte_tx = 8'd0;
        endcase
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= 5'd0;
            return_state <= 5'd1;
            txn_op <= 3'd0;
            txn_addr <= 24'd0;
            txn_index <= 5'd0;
            gap_count <= 2'd0;
            byte_start <= 1'b0;
            flash_cs_n <= 1'b1;
            id0 <= 8'd0; id1 <= 8'd0; id2 <= 8'd0;
            status_byte <= 8'd0;
            record_buf <= 144'd0;
            write_record <= 144'd0;
            scan_group <= 4'd0;
            scan_slot <= 1'b0;
            pending_group <= 4'd0;
            pending_slot <= 1'b0;
            pending_generation <= 8'd0;
            candidate_valid <= 1'b0;
            candidate_slot <= 1'b0;
            candidate_generation <= 8'd0;
            candidate_positions <= 50'd0;
            latest_valid <= 9'd0;
            latest_slot <= 9'd0;
            latest_generation <= 72'd0;
            poll_count <= 16'd0;
            init_done <= 1'b0;
            available <= 1'b0;
            load_valid <= 1'b0;
            load_group <= 4'd0;
            load_positions <= 50'd0;
            save_done <= 1'b0;
            save_ok <= 1'b0;
        end else begin
            byte_start <= 1'b0;
            load_valid <= 1'b0;
            save_done <= 1'b0;
            case (state)
                5'd0: begin
                    txn_op <= 3'd0;
                    return_state <= 5'd1;
                    state <= 5'd14;
                end
                5'd1: begin
                    if ((id0 == 8'h68) && (id1 == 8'h40) && (id2 == 8'h15)) begin
                        available <= 1'b1;
                        scan_group <= 4'd0;
                        scan_slot <= 1'b0;
                        candidate_valid <= 1'b0;
                        txn_op <= 3'd1;
                        txn_addr <= sector_address(4'd0, 1'b0);
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end else begin
                        init_done <= 1'b1;
                        state <= 5'd5;
                    end
                end
                5'd2: begin
                    if (valid_record &&
                        (!candidate_valid || newer_record)) begin
                        candidate_valid <= 1'b1;
                        candidate_slot <= scan_slot;
                        candidate_generation <= record_buf[103:96];
                        candidate_positions <= decode_positions(record_buf[95:16]);
                    end
                    if (!scan_slot) begin
                        scan_slot <= 1'b1;
                        txn_addr <= sector_address(scan_group, 1'b1);
                        txn_op <= 3'd1;
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end else state <= 5'd3;
                end
                5'd3: begin
                    if (candidate_valid) begin
                        latest_valid[scan_group] <= 1'b1;
                        latest_slot[scan_group] <= candidate_slot;
                        latest_generation[scan_group * 8 +: 8] <= candidate_generation;
                        load_valid <= 1'b1;
                        load_group <= scan_group;
                        load_positions <= candidate_positions;
                    end
                    if (scan_group == 4'd8) state <= 5'd4;
                    else begin
                        scan_group <= scan_group + 1'b1;
                        scan_slot <= 1'b0;
                        candidate_valid <= 1'b0;
                        txn_addr <= sector_address(scan_group + 1'b1, 1'b0);
                        txn_op <= 3'd1;
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end
                end
                5'd4: begin
                    init_done <= 1'b1;
                    state <= 5'd5;
                end
                5'd5: begin
                    if (save_req) begin
                        if (available && (save_group <= 4'd8)) begin
                            pending_group <= save_group;
                            pending_slot <= latest_valid[save_group] ? !latest_slot[save_group] : 1'b0;
                            pending_generation <= latest_valid[save_group] ?
                                latest_generation[save_group * 8 +: 8] + 1'b1 : 8'd0;
                            write_record <= make_record(save_group,
                                latest_valid[save_group] ? latest_generation[save_group * 8 +: 8] + 1'b1 : 8'd0,
                                save_positions);
                            txn_addr <= sector_address(save_group,
                                latest_valid[save_group] ? !latest_slot[save_group] : 1'b0);
                            txn_op <= 3'd2;
                            return_state <= 5'd6;
                            state <= 5'd14;
                        end else state <= 5'd13;
                    end
                end
                5'd6: begin
                    txn_op <= 3'd3;
                    return_state <= 5'd7;
                    state <= 5'd14;
                end
                5'd7: begin
                    poll_count <= 16'd0;
                    txn_op <= 3'd4;
                    return_state <= 5'd8;
                    state <= 5'd14;
                end
                5'd8: begin
                    if (status_byte[0]) begin
                        if (poll_count == 16'd60000) state <= 5'd13;
                        else begin
                            poll_count <= poll_count + 1'b1;
                            txn_op <= 3'd4;
                            return_state <= 5'd8;
                            state <= 5'd14;
                        end
                    end else begin
                        txn_op <= 3'd2;
                            return_state <= 5'd9;
                        state <= 5'd14;
                    end
                end
                5'd9: begin
                    txn_op <= 3'd5;
                    return_state <= 5'd10;
                    state <= 5'd14;
                end
                5'd10: begin
                    poll_count <= 16'd0;
                    txn_op <= 3'd4;
                    return_state <= 5'd11;
                    state <= 5'd14;
                end
                5'd11: begin
                    if (status_byte[0]) begin
                        if (poll_count == 16'd60000) state <= 5'd13;
                        else begin
                            poll_count <= poll_count + 1'b1;
                            txn_op <= 3'd4;
                            return_state <= 5'd11;
                            state <= 5'd14;
                        end
                    end else begin
                        txn_op <= 3'd1;
                        return_state <= 5'd12;
                        state <= 5'd14;
                    end
                end
                5'd12: begin
                    if (record_buf == write_record) begin
                        latest_valid[pending_group] <= 1'b1;
                        latest_slot[pending_group] <= pending_slot;
                        latest_generation[pending_group * 8 +: 8] <= pending_generation;
                        save_ok <= 1'b1;
                        save_done <= 1'b1;
                        state <= 5'd5;
                    end else state <= 5'd13;
                end
                5'd13: begin
                    save_ok <= 1'b0;
                    save_done <= 1'b1;
                    state <= 5'd5;
                end
                5'd14: begin
                    flash_cs_n <= 1'b0;
                    txn_index <= 5'd0;
                    state <= 5'd15;
                end
                5'd15: begin
                    if (!byte_busy && !byte_start) begin
                        byte_start <= 1'b1;
                        state <= 5'd16;
                    end
                end
                5'd16: begin
                    if (byte_done) begin
                        if (txn_op == 3'd0) begin
                            case (txn_index)
                                5'd1: id0 <= byte_rx;
                                5'd2: id1 <= byte_rx;
                                5'd3: id2 <= byte_rx;
                                default: begin end
                            endcase
                        end else if ((txn_op == 3'd1) && (txn_index >= 5'd4))
                            record_buf <= {record_buf[135:0], byte_rx};
                        else if ((txn_op == 3'd4) && (txn_index == 5'd1))
                            status_byte <= byte_rx;

                        if (((txn_op == 3'd0 || txn_op == 3'd3) && txn_index == 5'd3) ||
                            (txn_op == 3'd2 && txn_index == 5'd0) ||
                            (txn_op == 3'd4 && txn_index == 5'd1) ||
                            ((txn_op == 3'd1 || txn_op == 3'd5) && txn_index == 5'd21)) begin
                            flash_cs_n <= 1'b1;
                            gap_count <= 2'd0;
                            state <= 5'd17;
                        end else begin
                            txn_index <= txn_index + 1'b1;
                            state <= 5'd15;
                        end
                    end
                end
                5'd17: begin
                    if (gap_count == 2'd3) state <= return_state;
                    else gap_count <= gap_count + 1'b1;
                end
                default: state <= 5'd0;
            endcase
        end
    end
endmodule

`timescale 1ns/1ps
module b_tasks_switch #(parameter integer STABLE_CLKS=480000)(
 input wire clk,rst_n,display_sw,initialized,output wire enabled
);
 (* async_reg="true",preserve="true" *)reg sw_meta,sw_sync;
 reg state;reg [19:0] count;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin sw_meta<=0;sw_sync<=0;state<=0;count<=0;end
  else begin
   // Switch ON closes to ground on the v1.0 schematic; fail closed at reset.
   sw_meta<=!display_sw;sw_sync<=sw_meta;
   if(sw_sync==state)count<=0;
   else if(count==STABLE_CLKS-1)begin state<=sw_sync;count<=0;end
   else count<=count+1'b1;
  end
 end
 assign enabled=state && initialized;
endmodule

`timescale 1ns/1ps
`default_nettype none
// RAW stays in ADC mV in history: the renderer subtracts the visible mean.
// Other pages are normalized to native trace units (full lane = +/-22966).
module b_tasks_scale(
 input wire clk,rst_n,sample_valid,segment_break,
 input wire [127:0] raw_mv,input wire [191:0] filtered_q8,fast_q4,slow_q4,
 input wire [2:0] page,input wire [127:0] ranges,gains,input wire [7:0] custom,
 input wire [15:0] revision,input wire fifo_full,
 output reg valid,output reg [160:0] data,output reg saturated,
 output reg [31:0] dropped,output reg [15:0] busy_drops,full_drops
);
 reg [191:0] values;reg [127:0] range_hold,gain_hold,scaled;
 reg [7:0] custom_hold;reg [2:0] page_hold,lane;reg [15:0] rev_hold;
 reg working,emit_pending,clear_pending,sat;
 reg [2:0] phase;reg [15:0] range_lane,gain_lane;reg custom_lane;
 reg signed [24:0] value;reg [23:0] factor;
 reg signed [49:0] product;reg signed [34:0] quantized,normalized;
 // Rounded Q12 reciprocals: error < 0.5 native unit at every standard bound.
 function [23:0] coefficient;input [15:0] r;
  begin case(r)
   20:coefficient=4703437;50:coefficient=1881375;100:coefficient=940687;
   200:coefficient=470344;500:coefficient=188137;1200:coefficient=78391;
   default:coefficient=28506;
  endcase end
 endfunction
 integer k;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin values<=0;range_hold<=0;gain_hold<=0;scaled<=0;custom_hold<=0;page_hold<=0;
   lane<=0;rev_hold<=0;working<=0;emit_pending<=0;clear_pending<=1;valid<=0;data<=0;
   saturated<=0;dropped<=0;busy_drops<=0;full_drops<=0;sat<=0;
   phase<=0;range_lane<=0;gain_lane<=0;custom_lane<=0;value<=0;factor<=0;product<=0;quantized<=0;normalized<=0;end
  else begin
   valid<=0;
   if(segment_break)clear_pending<=1;
   if(sample_valid && page<4)begin
    if(working || emit_pending)begin dropped<=dropped+1'b1;busy_drops<=busy_drops+1'b1;clear_pending<=1;end
    else begin
     for(k=0;k<8;k=k+1)case(page)
      0:values[k*24+:24]<={8'd0,raw_mv[k*16+:16]};
      1:values[k*24+:24]<=filtered_q8[k*24+:24];
      2:values[k*24+:24]<=fast_q4[k*24+:24];
      3:values[k*24+:24]<=slow_q4[k*24+:24];
     endcase
     range_hold<=ranges;gain_hold<=gains;custom_hold<=custom;page_hold<=page;rev_hold<=revision;
     lane<=0;phase<=0;working<=1;sat<=0;
    end
   end
   // One channel uses six short cycles, under 3us for all eight at 24MHz.
   // Preserve exact rounding and clipping; scaling never changes input data.
   if(working)case(phase)
    0:begin
     value<=page_hold==1?$signed({values[lane*24+23],values[lane*24+:24]}):$signed({1'b0,values[lane*24+:24]});
     range_lane<=range_hold[lane*16+:16];gain_lane<=gain_hold[lane*16+:16];custom_lane<=custom_hold[lane];phase<=1;
    end
    1:begin factor<=custom_lane?{8'd0,gain_lane}:coefficient(range_lane);phase<=2;end
    2:begin product<=value*$signed({1'b0,factor});phase<=3;end
    3:begin
     if(custom_lane)quantized<=page_hold==1?(product+2048)>>>12:(product+128)>>>8;
     else quantized<=page_hold==1?(product+524288)>>>20:(product+32768)>>>16;
     phase<=4;
    end
    4:begin normalized<=page_hold>=2?(quantized<<<1)-22966:quantized;phase<=5;end
    5:begin
     if(page_hold==0)scaled[lane*16+:16]<=value[15:0];
     else begin
      scaled[lane*16+:16]<=normalized>22966?16'sd22966:normalized< -22966?-16'sd22966:normalized[15:0];
      if(normalized>22966 || normalized< -22966)sat<=1;
     end
     phase<=0;
     if(lane==7)begin working<=0;emit_pending<=1;end else lane<=lane+1'b1;
    end
   endcase
   if(emit_pending)begin
    emit_pending<=0;
    if(fifo_full)begin dropped<=dropped+1'b1;full_drops<=full_drops+1'b1;clear_pending<=1;end
    else begin data<={16'd0,rev_hold,clear_pending||segment_break,scaled};valid<=1;clear_pending<=0;saturated<=sat;end
   end
  end
 end
endmodule
`default_nettype wire

`timescale 1ns/1ps

module b_tasks_engine #(
    
    parameter integer RESPONSE_TIMEOUT_CLKS = 48000
) (
    input  wire        clk,
    input  wire        rst_n,
    input wire [7:0] SERVO_ID,
    input wire [15:0] command_speed,
    output reg [7:0] torque_value,
    output reg [391:0] configuration,
    input  wire        command_valid,
    input  wire [2:0]  command,
    input  wire [9:0]  target_position,
    input  wire        servo_rx,
    output wire        servo_tx,
    output reg         tx_enable,
    output reg         busy,
    output reg         response_valid,
    output reg  [2:0]  response_command,
    output reg  [3:0]  result_code,
    output reg  [7:0]  servo_error,
    // Position from the response that raised response_valid.  This is
    // separate from present_position because both registers are committed
    // on the same clock edge.
    output reg  [15:0] response_position,
    output reg  [15:0] present_position,
    output reg  [15:0] present_speed,
    output reg  [15:0] present_load,
    output reg  [15:0] present_current,
    output reg  [7:0]  present_voltage,
    output reg  [7:0]  present_temperature,
    output reg         online
);

    localparam [2:0] CMD_PING       = 3'd0;
    localparam [2:0] CMD_READ_STATE = 3'd1;
    localparam [2:0] CMD_TORQUE_OFF = 3'd2;
    localparam [2:0] CMD_TORQUE_ON  = 3'd3;
    localparam [2:0] CMD_MOVE       = 3'd4;
    localparam [2:0] CMD_HOLD_HIGH  = 3'd5;
    localparam [2:0] CMD_HOLD_LOW   = 3'd6;
    localparam [2:0] CMD_SLOW_MOVE  = 3'd7;

    localparam [2:0] ST_IDLE = 3'd0;
    localparam [2:0] ST_TX   = 3'd1;
    localparam [2:0] ST_RX   = 3'd2;

    localparam [2:0] PS_HEAD1 = 3'd0;
    localparam [2:0] PS_HEAD2 = 3'd1;
    localparam [2:0] PS_ID    = 3'd2;
    localparam [2:0] PS_LEN   = 3'd3;
    localparam [2:0] PS_ERROR = 3'd4;
    localparam [2:0] PS_PARAM = 3'd5;
    localparam [2:0] PS_CHECK = 3'd6;

    reg [2:0] state;
    reg [2:0] parser_state;
    reg [2:0] command_latched;
    reg [9:0] target_latched;reg [15:0] speed_latched;reg [7:0] torque_temp;
    reg [3:0] tx_length;
    reg [3:0] tx_index;
    reg [7:0] selected_tx_byte;
    reg tx_issued;
    reg tx_start;
    reg [7:0] tx_data;
    wire tx_busy;
    wire tx_done;

    reg [20:0] timeout_count;
    reg [7:0] response_length;
    reg [7:0] checksum_sum;
    reg [7:0] parameter_index;
    reg [7:0] received_byte_count;
    reg [15:0] temp_position;
    reg [15:0] temp_speed;
    reg [15:0] temp_load;
    reg [15:0] temp_current;
    reg [7:0] temp_voltage;
    reg [7:0] temp_temperature;

    wire [7:0] rx_data;
    wire rx_valid;
    wire rx_busy;
    wire rx_framing_error;
    wire rx_enable;

    assign rx_enable = (state == ST_RX);

    always @(*) begin
        selected_tx_byte = 8'hff;
        if (tx_index == 0)
            selected_tx_byte = 8'hff;
        else if (tx_index == 1)
            selected_tx_byte = 8'hff;
        else if (tx_index == 2)
            selected_tx_byte = SERVO_ID;
        else begin
            case (command_latched)
                CMD_PING: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd2;
                        4: selected_tx_byte = 8'h01;
                        5: selected_tx_byte = ~(SERVO_ID + 8'd2 + 8'h01);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_READ_STATE: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h02;
                        5: selected_tx_byte = 8'd56;
                        6: selected_tx_byte = 8'd15;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h02 + 8'd56 + 8'd15);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_TORQUE_OFF: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h03;
                        5: selected_tx_byte = 8'd40;
                        6: selected_tx_byte = 8'd0;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h03 + 8'd40);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_TORQUE_ON: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h03;
                        5: selected_tx_byte = 8'd40;
                        6: selected_tx_byte = 8'd1;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h03 + 8'd40 + 8'd1);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_MOVE, CMD_SLOW_MOVE: begin
                    case (tx_index)
                        3:  selected_tx_byte = 8'd9;
                        4:  selected_tx_byte = 8'h03;
                        5:  selected_tx_byte = 8'd42;
                        6:  selected_tx_byte = {6'd0, target_latched[9:8]};
                        7:  selected_tx_byte = target_latched[7:0];
                        8:  selected_tx_byte = 8'd0;
                        9:  selected_tx_byte = 8'd0;
                        10: selected_tx_byte = speed_latched[15:8];
                        11: selected_tx_byte = speed_latched[7:0];
                        12: selected_tx_byte = ~(SERVO_ID + 8'd9 + 8'h03 + 8'd42 +
                                                  {6'd0, target_latched[9:8]} +
                                                  target_latched[7:0] +
                                                  speed_latched[15:8]+speed_latched[7:0]);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_HOLD_HIGH, CMD_HOLD_LOW: begin
                    case(tx_index)
                     3:selected_tx_byte=4;
                     4:selected_tx_byte=2;
                     5:selected_tx_byte=command_latched==CMD_HOLD_HIGH?0:40;
                     6:selected_tx_byte=command_latched==CMD_HOLD_HIGH?49:1;
                     7:selected_tx_byte=~(SERVO_ID+8'd4+8'd2+(command_latched==CMD_HOLD_LOW?8'd41:8'd49));
                     default:selected_tx_byte=8'hff;
                    endcase
                end
                default: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd2;
                        4: selected_tx_byte = 8'h01;
                        5: selected_tx_byte = ~(SERVO_ID + 8'd2 + 8'h01);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end
            endcase
        end
    end

    uart_tx_byte #(
        .CLKS_PER_BIT(24)
    ) servo_uart_tx (
        .clk   (clk),
        .rst_n (rst_n),
        .start (tx_start),
        .data  (tx_data),
        .tx    (servo_tx),
        .busy  (tx_busy),
        .done  (tx_done)
    );

    uart_rx_byte #(
        .CLKS_PER_BIT(24)
    ) servo_uart_rx (
        .clk           (clk),
        .rst_n         (rst_n),
        .enable        (rx_enable),
        .rx            (servo_rx),
        .data          (rx_data),
        .valid         (rx_valid),
        .busy          (rx_busy),
        .framing_error (rx_framing_error)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state               <= ST_IDLE;
            parser_state        <= PS_HEAD1;
            command_latched     <= CMD_PING;
            target_latched      <= 10'd512;
            tx_length           <= 4'd0;
            tx_index            <= 4'd0;
            tx_issued           <= 1'b0;
            tx_start            <= 1'b0;
            tx_data             <= 8'hff;
            tx_enable           <= 1'b0;
            busy                <= 1'b0;
            response_valid      <= 1'b0;
            response_command    <= CMD_PING;
            result_code         <= 4'd0;
            servo_error         <= 8'd0;
            response_position   <= 16'd0;
            present_position    <= 16'd0;
            present_speed       <= 16'd0;
            present_load        <= 16'd0;
            present_current     <= 16'd0;
            present_voltage     <= 8'd0;
            present_temperature <= 8'd0;
            online              <= 1'b0;
            timeout_count       <= 21'd0;
            response_length     <= 8'd0;
            checksum_sum        <= 8'd0;
            parameter_index     <= 8'd0;
            received_byte_count <= 8'd0;
            temp_position       <= 16'd0;
            temp_speed          <= 16'd0;
            temp_load           <= 16'd0;
            temp_current        <= 16'd0;
            temp_voltage        <= 8'd0;
            temp_temperature    <= 8'd0;
        end else begin
            tx_start       <= 1'b0;
            response_valid <= 1'b0;

            case (state)
                ST_IDLE: begin
                    busy      <= 1'b0;
                    tx_enable <= 1'b0;
                    tx_issued <= 1'b0;

                    if (command_valid) begin
                        busy              <= 1'b1;
                        tx_enable         <= 1'b1;
                        command_latched   <= command;
                        target_latched    <= target_position;speed_latched<=command_speed;
                        response_command  <= command;
                        result_code       <= 4'd0;
                        servo_error       <= 8'd0;
                        tx_index          <= 4'd0;
                        tx_issued         <= 1'b0;
                        timeout_count     <= 21'd0;
                        parser_state      <= PS_HEAD1;
                        parameter_index   <= 8'd0;
                        received_byte_count <= 8'd0;
                        state             <= ST_TX;

                        case (command)
                            CMD_PING:       tx_length <= 4'd6;
                            CMD_READ_STATE: tx_length <= 4'd8;
                            CMD_TORQUE_OFF: tx_length <= 4'd8;
                            CMD_TORQUE_ON:  tx_length <= 4'd8;
                            CMD_MOVE, CMD_SLOW_MOVE: tx_length <= 4'd13;
                            CMD_HOLD_HIGH:  tx_length <= 4'd8;
                            CMD_HOLD_LOW:   tx_length <= 4'd8;
                            default:        tx_length <= 4'd6;
                        endcase
                    end
                end

                ST_TX: begin
                    if (!tx_issued && !tx_busy) begin
                        tx_data   <= selected_tx_byte;
                        tx_start  <= 1'b1;
                        tx_issued <= 1'b1;
                    end

                    if (tx_done && tx_issued) begin
                        tx_issued <= 1'b0;
                        if (tx_index == tx_length - 1'b1) begin
                            tx_enable     <= 1'b0;
                            timeout_count <= 21'd0;
                            parser_state  <= PS_HEAD1;
                            state <= ST_RX;
                        end else begin
                            tx_index <= tx_index + 1'b1;
                        end
                    end
                end

                ST_RX: begin
                    if (timeout_count == RESPONSE_TIMEOUT_CLKS - 1) begin
                        state            <= ST_IDLE;
                        busy             <= 1'b0;
                        response_valid   <= 1'b1;
                        response_command <= command_latched;
                        result_code      <= 4'd1;
                        servo_error      <= received_byte_count;
                        online           <= 1'b0;
                    end else begin
                        timeout_count <= timeout_count + 1'b1;
                    end

                    if (rx_framing_error) begin
                        state            <= ST_IDLE;
                        busy             <= 1'b0;
                        response_valid   <= 1'b1;
                        response_command <= command_latched;
                        result_code      <= 4'd3;
                    end else if (rx_valid) begin
                        if (received_byte_count != 8'hff)
                            received_byte_count <= received_byte_count + 1'b1;
                        case (parser_state)
                            PS_HEAD1: begin
                                if (rx_data == 8'hff)
                                    parser_state <= PS_HEAD2;
                            end

                            PS_HEAD2: begin
                                if (rx_data == 8'hff)
                                    parser_state <= PS_ID;
                                else
                                    parser_state <= PS_HEAD1;
                            end

                            PS_ID: begin
                                if (rx_data == SERVO_ID) begin
                                    checksum_sum <= rx_data;
                                    parser_state <= PS_LEN;
                                end else begin
                                    parser_state <= PS_HEAD1;
                                end
                            end

                            PS_LEN: begin
                                if ((rx_data >= 8'd2) && (rx_data <= 8'd51)) begin
                                    response_length <= rx_data;
                                    checksum_sum    <= checksum_sum + rx_data;
                                    parser_state    <= PS_ERROR;
                                end else begin
                                    state            <= ST_IDLE;
                                    busy             <= 1'b0;
                                    response_valid   <= 1'b1;
                                    response_command <= command_latched;
                                    result_code      <= 4'd3;
                                end
                            end

                            PS_ERROR: begin
                                servo_error  <= rx_data;
                                checksum_sum <= checksum_sum + rx_data;
                                if (response_length == 8'd2)
                                    parser_state <= PS_CHECK;
                                else begin
                                    parameter_index <= 8'd0;
                                    parser_state    <= PS_PARAM;
                                end
                            end

                            PS_PARAM: begin
                                checksum_sum <= checksum_sum + rx_data;
                                if(command_latched==CMD_HOLD_HIGH)configuration[parameter_index*8+:8]<=rx_data;
                                if(command_latched==CMD_HOLD_LOW)torque_temp<=rx_data;
                                case (parameter_index)
                                    0: temp_position[15:8] <= rx_data;
                                    1: temp_position[7:0]  <= rx_data;
                                    2: temp_speed[15:8]    <= rx_data;
                                    3: temp_speed[7:0]     <= rx_data;
                                    4: temp_load[15:8]     <= rx_data;
                                    5: temp_load[7:0]      <= rx_data;
                                    6: temp_voltage         <= rx_data;
                                    7: temp_temperature     <= rx_data;
                                    // SCSCL End=1: address 69 is the high byte,
                                    // address 70 the low byte; bit 15 is sign.
                                    13: temp_current[15:8]   <= rx_data;
                                    14: temp_current[7:0]    <= rx_data;
                                    default: temp_temperature <= temp_temperature;
                                endcase

                                if (parameter_index == response_length - 8'd3)
                                    parser_state <= PS_CHECK;
                                else
                                    parameter_index <= parameter_index + 1'b1;
                            end

                            PS_CHECK: begin
                                state            <= ST_IDLE;
                                busy             <= 1'b0;
                                response_valid   <= 1'b1;
                                response_command <= command_latched;

                                if (rx_data != ~checksum_sum) begin
                                    result_code <= 4'd2;
                                end else if (((command_latched == CMD_READ_STATE) &&
                                              (response_length != 8'd17)) ||
                                             ((command_latched == CMD_HOLD_HIGH) && response_length!=51) ||
                                             ((command_latched == CMD_HOLD_LOW) && response_length!=3) ||
                                             ((command_latched != CMD_READ_STATE && command_latched != CMD_HOLD_HIGH && command_latched != CMD_HOLD_LOW) && response_length!=2)) begin
                                    result_code <= 4'd3;
                                end else begin
                                    online      <= 1'b1;
                                    result_code <= ((servo_error & 8'hfe) == 0) ? 4'd0 : 4'd4;
                                    
                                    if(command_latched==CMD_HOLD_LOW)torque_value<=torque_temp;
                                    if (command_latched == CMD_READ_STATE) begin
                                        response_position  <= temp_position;
                                        present_position    <= temp_position;
                                        present_speed       <= temp_speed;
                                        present_load        <= temp_load;
                                        present_current     <= temp_current;
                                        present_voltage     <= temp_voltage;
                                        present_temperature <= temp_temperature;
                                    end
                                end
                            end

                            default: parser_state <= PS_HEAD1;
                        endcase
                    end
                end

                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule

`timescale 1ns/1ps
`default_nettype none
// One shared half-duplex engine. All STOP/teach completions require readback.
module b_tasks_servo #(
 parameter integer POLL_CLKS=24000,TIMEOUT_CLKS=480000,GAP_CLKS=2400,MS_CLKS=24000
)(
 input wire clk,rst_n,servo_rx,output wire servo_tx,servo_tx_enable,
 input wire req_valid,output wire req_ready,input wire [2:0] req_op,
 input wire [15:0] req_id,req_arg,
 output reg event_valid,input wire event_ready,output reg [15:0] event_id,event_status,
 output wire idle,input wire [3:0] query_pose,output wire query_valid,output wire [49:0] query_positions,
 input wire flash_init_done,flash_available,flash_load_valid,input wire [3:0] flash_load_group,
 input wire [49:0] flash_load_positions,output reg flash_save_req,output reg [3:0] flash_save_group,
 output reg [49:0] flash_save_positions,input wire flash_save_done,flash_save_ok,
 output reg [4:0] online,torque,torque_valid_mask,output reg [79:0] positions,speeds,loads,
 output wire [79:0] feedback_age_ms,output wire [4:0] feedback_valid_mask,
 output reg [8:0] pose_valid,output reg [7:0] motion_state,
 output wire [4:0] active_mask,retired_mask,output reg [31:0] fault_flags
);
 localparam IDLE=0,OFF=1,VERIFY_OFF=2,CAPTURE=3,SAVE=4,GOAL=5,ON=6,TRACK=7,POLL=8,FETCH_POSE=9;
 reg [3:0] state;reg [2:0] lane;reg [15:0] current_id;reg pending,failed,stop_wait;
 reg [15:0] waiting_stop_id;reg [3:0] pose;reg [49:0] captured;
 localparam GAP_BITS=$clog2(GAP_CLKS+1),POLL_BITS=$clog2(POLL_CLKS+1);
 (* ram_style="block" *)reg [49:0] poses[0:8];
 reg [49:0] pose_read_data,current_pose;
 wire fetch_pose=req_valid && req_ready && req_op==2;
 wire [3:0] pose_read_address=fetch_pose?req_arg[3:0]:query_pose;
 always @(posedge clk)begin
  pose_read_data<=poses[pose_read_address];
  if(flash_load_valid && flash_load_group<9)poses[flash_load_group]<=flash_load_positions;
  else if(save_inflight && flash_save_done && flash_save_ok)poses[flash_save_group]<=flash_save_positions;
 end
 reg [GAP_BITS-1:0] gap;reg [POLL_BITS-1:0] poll_age;
 reg [4:0] arrived;reg [9:0] best[0:4];reg [15:0] progress_stamp[0:4],motion_started,now_ms;
 wire [15:0] motion_ms=now_ms-motion_started,progress_ms=now_ms-progress_stamp[lane];
 reg [15:0] feedback_age[0:4];reg [14:0] ms_counter;
 genvar age_lane;
 generate for(age_lane=0;age_lane<5;age_lane=age_lane+1)begin:ages
  assign feedback_age_ms[age_lane*16+:16]=feedback_age[age_lane];
  assign feedback_valid_mask[age_lane]=online[age_lane] && feedback_age[age_lane]<200;
 end endgenerate
 reg start,inflight;reg [2:0] command;reg [9:0] target;reg [15:0] speed;
 wire engine_busy,done;wire [3:0] result;wire [7:0] error,voltage,temp,torque_read;
 wire [15:0] pos,vel,load;wire [391:0] configuration;
 reg save_inflight;
 wire response_ok=result==0;
 wire [9:0] goal=current_pose[lane*10+:10];
 wire [15:0] distance=pos>=goal?pos-goal:goal-pos;
 b_tasks_engine #(.RESPONSE_TIMEOUT_CLKS(TIMEOUT_CLKS)) engine(
  .clk(clk),.rst_n(rst_n),.SERVO_ID({5'd0,lane}+8'd1),.command_speed(speed),
  .command_valid(start),.command(command),.target_position(target),.servo_rx(servo_rx),
  .servo_tx(servo_tx),.tx_enable(servo_tx_enable),.busy(engine_busy),.response_valid(done),
  .result_code(result),.servo_error(error),.response_position(pos),.present_speed(vel),
  .present_load(load),.present_voltage(voltage),.present_temperature(temp),
  .torque_value(torque_read),.configuration(configuration));
 assign idle=state==IDLE && !pending && !stop_wait && !event_valid && !save_inflight && flash_init_done;
 assign req_ready=req_op==3 ? !stop_wait : idle;
 assign query_valid=query_pose<9?pose_valid[query_pose]:0;
 assign query_positions=query_valid?pose_read_data:0;
 assign active_mask=motion_state==1?5'b11111:0;
 assign retired_mask=5'd0;
 task finish;input [15:0] status;begin
  state<=IDLE;motion_state<=0;
  if(pending)begin event_valid<=1;event_id<=current_id;event_status<=status;pending<=0;end
 end endtask
 task launch;input [2:0] c;input [9:0] g;input [15:0] s;begin
  command<=c;target<=g;speed<=s;start<=1;inflight<=1;
 end endtask
 integer i;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   state<=OFF;lane<=0;current_id<=0;pending<=0;failed<=0;stop_wait<=0;waiting_stop_id<=0;
   pose<=0;captured<=0;current_pose<=0;gap<=0;poll_age<=0;motion_started<=0;now_ms<=0;arrived<=0;
   start<=0;inflight<=0;command<=0;target<=0;speed<=20;
   save_inflight<=0;
   online<=0;torque<=0;torque_valid_mask<=0;positions<=0;speeds<=0;loads<=0;pose_valid<=0;motion_state<=0;fault_flags<=0;
   event_valid<=0;event_id<=0;event_status<=0;flash_save_req<=0;flash_save_group<=0;flash_save_positions<=0;
   ms_counter<=0;
   for(i=0;i<5;i=i+1)begin best[i]<=1023;progress_stamp[i]<=0;feedback_age[i]<=16'hffff;end
  end else begin
   start<=0;flash_save_req<=0;
   if(event_valid && event_ready)event_valid<=0;
   if(ms_counter==MS_CLKS-1)begin
    ms_counter<=0;now_ms<=now_ms+1'b1;for(i=0;i<5;i=i+1)if(feedback_age[i]!=16'hffff)feedback_age[i]<=feedback_age[i]+1'b1;
   end else ms_counter<=ms_counter+1'b1;
   if(gap!=0)gap<=gap-1'b1;
   if(poll_age<POLL_CLKS)poll_age<=poll_age+1'b1;
   if(motion_state==1)begin
    if(motion_ms>=10000)begin failed<=1;fault_flags[2]<=1;state<=OFF;lane<=0;motion_state<=0;end
   end
   if(flash_load_valid && flash_load_group<9)pose_valid[flash_load_group]<=1;
   // Flash completion is independent of the SCS inter-packet quiet interval.
   if(save_inflight && flash_save_done)begin
    save_inflight<=0;
    if(flash_save_ok)pose_valid[flash_save_group]<=1;
    else fault_flags[3]<=1;
    if(state==SAVE)finish(flash_save_ok?0:7);
   end
   if(stop_wait && !event_valid && !inflight && !engine_busy && gap==0)begin
    stop_wait<=0;pending<=1;current_id<=waiting_stop_id;failed<=0;state<=OFF;lane<=0;motion_state<=0;
   end else if( !inflight && !engine_busy && !start && gap==0)begin
    case(state)
     IDLE:if(poll_age>=POLL_CLKS)begin state<=POLL;lane<=0;poll_age<=0;end
     OFF:launch(2,0,20);
     VERIFY_OFF:launch(6,0,20);
     CAPTURE:launch(1,0,20);
     SAVE:begin end
     GOAL:launch(4,goal,20);
     FETCH_POSE:begin current_pose<=pose_read_data;state<=GOAL;end
     ON:launch(3,0,20);
     TRACK,POLL:launch(1,0,20);
    endcase
   end
   if(done && inflight)begin
    inflight<=0;gap<=GAP_CLKS;online[lane]<=response_ok;
    if(command==2 || command==3 || command==6)begin
     torque_valid_mask[lane]<=response_ok;
     if(response_ok)torque[lane]<=command==6?torque_read!=0:command==3;
    end
    if(error[0])fault_flags[8]<=1; // Voltage warning is observation only.
    if(command==1 && response_ok)begin
     positions[lane*16+:16]<=pos;speeds[lane*16+:16]<=vel;loads[lane*16+:16]<=load;
     feedback_age[lane]<=0;
    end
    case(state)
     OFF:begin
      if(!response_ok)failed<=1;
      state<=VERIFY_OFF;
     end
     VERIFY_OFF:begin
      if(!response_ok || torque_read!=0)begin failed<=1;fault_flags[0]<=1;end
      else torque[lane]<=0;
      if(lane==4)finish((failed || !response_ok || torque_read!=0)?7:0);
      else begin lane<=lane+1'b1;state<=OFF;end
     end
     CAPTURE:begin
      if(!response_ok || pos>1023)begin finish(7);fault_flags[1]<=1;end
      else begin
       captured[lane*10+:10]<=pos[9:0];
       if(lane==4)begin
        flash_save_req<=1;flash_save_group<=pose;flash_save_positions<={pos[9:0],captured[39:0]};
        save_inflight<=1;
        state<=SAVE;
       end else lane<=lane+1'b1;
      end
     end
     GOAL:begin
      if(!response_ok)begin failed<=1;state<=OFF;lane<=0;end
      else begin state<=ON;end
     end
     ON:begin
      if(!response_ok)begin failed<=1;state<=OFF;lane<=0;end
      else begin
       torque[lane]<=1;
       if(lane==4)begin lane<=0;state<=TRACK;end
       else begin lane<=lane+1'b1;state<=GOAL;end
      end
     end
     TRACK:begin
      if(!response_ok || temp>=45 || pos>1023 || load[9:0]>=300 || progress_ms>=1000)begin
       failed<=1;fault_flags[2]<=1;state<=OFF;lane<=0;
      end else begin
       if(distance<=4)begin arrived[lane]<=1;progress_stamp[lane]<=now_ms;end
       else begin
        arrived[lane]<=0;
        if(distance+2<=best[lane])begin best[lane]<=distance[9:0];progress_stamp[lane]<=now_ms;end
       end
       if(&arrived && distance<=4)finish(0);
       else lane<=lane==4?0:lane+1'b1;
      end
     end
     POLL:if(lane==4)state<=IDLE;else lane<=lane+1'b1;
    endcase
   end
   if(req_valid && req_ready)begin
    if(req_op==3)begin
     // Cancel the old operation first; STOP has its own final event.
     if(pending)begin event_valid<=1;event_id<=current_id;event_status<=7;pending<=0;end
     stop_wait<=1;waiting_stop_id<=req_id;
     motion_state<=0;
    end else begin
     current_id<=req_id;pending<=1;failed<=0;lane<=0;pose<=req_arg[3:0];
     if(req_op==1 && req_arg<9 && torque==0 && flash_available)begin state<=CAPTURE;motion_state<=3;end
     else if(req_op==2 && req_arg<9 && pose_valid[req_arg[3:0]])begin
      state<=FETCH_POSE;motion_state<=1;arrived<=0;motion_started<=now_ms;
      for(i=0;i<5;i=i+1)begin best[i]<=1023;progress_stamp[i]<=now_ms;end
     end
     else begin event_valid<=1;event_id<=req_id;event_status<=5;pending<=0;end
    end
   end
  end
 end
endmodule
`default_nettype wire

`timescale 1ns/1ps
`default_nettype none
// r49: one authoritative typed map. Display authority is independent of ARM.
module b_tasks_control #(
 parameter integer FRESH_CLKS=7200000, MIN_EVENT_CLKS=4800000,
 parameter integer COLLISION_SUPPORTED=0
)(
 input wire clk,rst_n,display_enabled,
 input wire command_valid,output wire command_ready,
 input wire [15:0] request_id,opcode,data_length,input wire [79:0] arguments,
 output reg response_valid,input wire response_ready,output reg [7:0] response_type,
 output reg [15:0] response_id,response_opcode,response_status,
 output reg [9:0] response_length,output reg [4095:0] response_data,
 input wire [9:0] response_byte_index,output wire [7:0] response_byte,
 input wire result_valid,wave_valid,link_fault,stale,
 input wire [15:0] generation,epoch,class_id,class_count,input wire [7:0] result_flags,
 input wire [31:0] window_seq,
 input wire [4:0] online,torque,input wire [8:0] pose_valid,
 input wire servo_idle,flash_ready,flash_available,flash_initialized,input wire [7:0] motion_state,
 input wire [79:0] positions,speeds,loads,feedback_age_ms,input wire [4:0] feedback_valid_mask,torque_valid_mask,
 input wire [4:0] active_mask,retired_mask,input wire [31:0] fault_flags,
 output reg req_valid,input wire req_ready,output reg [2:0] req_op,
 output reg [15:0] req_id,req_arg,
 input wire event_valid,output wire event_ready,input wire [15:0] event_id,event_status,
 output wire [3:0] query_pose,input wire query_valid,input wire [49:0] query_positions,
 output reg [2:0] page,output reg freeze,armed,teach_mode,
 output reg [127:0] ranges,gains,output reg [7:0] custom,
 output reg [15:0] revision,
 output reg [31:0] auto_count,auto_rejects
);
 // Class ID is the row address; reserved fields are validated before storage.
 // Store only legal kind/enabled/pose bits, reconstruct the canonical wire row.
 reg [7:0] map0[0:31],map1[0:31];
 reg active_bank,mapping_valid,stage_started;
 reg [15:0] active_generation,active_count,stage_generation,stage_count;
 reg [31:0] map_revision,stage_revision,written;
 reg [2:0] range_pages[0:31];reg [12:0] gain_pages[0:31];reg [31:0] custom_pages;
 reg committing;reg [7:0] crc_index;reg [31:0] crc,expected_crc;
 reg [15:0] commit_id;
 reg normal_pending,stop_pending;reg [15:0] normal_id,normal_opcode,stop_id,stop_opcode;
 reg [1:0] pose_query_wait;reg [15:0] pose_query_id;
 reg teach_pending,fault_stop_latched,fault_stop_needed;
 reg [2:0] json_slot;
 reg [79:0] json_loads,json_ages;
 reg [127:0] json_words0,json_words1;
 wire [79:0] json_positions=json_words0[79:0],json_speeds=json_words1[79:0];
 wire [127:0] json_ranges=json_words0,json_gains=json_words1;
 wire [1:0] json_query_page=arguments[1:0]-2'd1;
 reg [4:0] json_online,json_torque,json_valid,json_torque_valid;
 reg [7:0] json_custom;reg [31:0] json_faults;
 (* ram_style="bram_32k" *)reg [15:0] query_tokens[0:4095];reg [15:0] query_token;
 initial $readmemh("E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_wave_fix_20261102/fpga_b/gesture_tasks/assets/query_tokens.hex",query_tokens);
 always @(posedge clk)query_token<=query_tokens[{json_slot,response_byte_index[8:0]}];
 reg [3:0] json_nibble;
 always @*begin
  json_nibble=0;
  case(query_token[11:8])
   1:json_nibble=json_positions[query_token[4:0]*4+:4];
   2:json_nibble=json_speeds[query_token[4:0]*4+:4];
   3:json_nibble=json_loads[query_token[4:0]*4+:4];
   4:json_nibble=json_ages[query_token[4:0]*4+:4];
   5:json_nibble=({3'd0,json_online} >> (query_token[0]*4));
   6:json_nibble=({3'd0,json_torque} >> (query_token[0]*4));
   7:json_nibble=({3'd0,json_valid} >> (query_token[0]*4));
   8:json_nibble=json_faults[query_token[2:0]*4+:4];
   9:json_nibble=json_ranges[query_token[4:0]*4+:4];
   10:json_nibble=json_gains[query_token[4:0]*4+:4];
   11:json_nibble=json_custom[query_token[0]*4+:4];
   12:json_nibble=({3'd0,json_torque_valid} >> (query_token[0]*4));
  endcase
 end
 assign response_byte=json_slot==7?response_data[response_byte_index[4:0]*8+:8]:
    query_token[11:8]==0?query_token[7:0]:hex_ascii(json_nibble);
 reg [31:0] result_age,event_age;reg result_seen,trigger_ready;
 reg [15:0] last_generation,last_epoch,last_class,last_class_count;reg [31:0] last_window;
 reg [1:0] stable_windows;reg [63:0] row;
 integer i;
 function [31:0] crc_byte;input [31:0] c;input [7:0] b;reg [31:0] v;integer k;
  begin v=c^b;for(k=0;k<8;k=k+1)v=v[0]?(v>>1)^32'hedb88320:v>>1;crc_byte=v;end
 endfunction
 function legal_range;input [15:0] r;
  begin legal_range=r==20 || r==50 || r==100 || r==200 || r==500 || r==1200 || r==3300;end
 endfunction
 function [2:0] range_code;input [15:0] r;
  begin case(r)20:range_code=0;50:range_code=1;100:range_code=2;200:range_code=3;
   500:range_code=4;1200:range_code=5;default:range_code=6;endcase end
 endfunction
 function [15:0] range_value;input [2:0] code;
  begin case(code)0:range_value=20;1:range_value=50;2:range_value=100;3:range_value=200;
   4:range_value=500;5:range_value=1200;default:range_value=3300;endcase end
 endfunction
 function [2:0] zoom_code;input [2:0] code;input inward;
  begin zoom_code=inward?(code==0?0:code-1'b1):(code>=6?6:code+1'b1);end
 endfunction
 function [7:0] hex_ascii;input [3:0] n;
  begin hex_ascii=n<10?8'd48+n:8'd87+n;end
 endfunction
 function [63:0] expand_row;input [7:0] entry;input [4:0] cls;
  begin expand_row={16'd0,12'd0,entry[7:4],7'd0,entry[3],5'd0,entry[2:0],11'd0,cls};end
 endfunction
 function [15:0] zoom;input [15:0] r;input inward;
  begin if(inward)case(r)20,50:zoom=20;100:zoom=50;200:zoom=100;500:zoom=200;1200:zoom=500;default:zoom=1200;endcase
  else case(r)20:zoom=50;50:zoom=100;100:zoom=200;200:zoom=500;500:zoom=1200;default:zoom=3300;endcase end
 endfunction
 function legal_row;input [63:0] r;
  begin legal_row=r[15:0]<stage_count && r[23:16]<=6 && (r[23:16]!=2 || COLLISION_SUPPORTED) && r[31:24]<=1 && r[63:48]==0 &&
   (r[23:16]==1 ? r[47:32]<9 : r[47:32]==0) &&
   (r[15:0]!=0 || (r[23:16]==0 && r[31:24]==0));end
 endfunction
 wire [63:0] stage_row=expand_row(active_bank?map0[crc_index[7:3]]:map1[crc_index[7:3]],crc_index[7:3]);
 wire [31:0] next_crc=crc_byte(crc,stage_row[crc_index[2:0]*8+:8]);
 wire [31:0] row_mask=32'hffffffff>>(32-stage_count);
 wire model_matches=mapping_valid && active_generation==generation && active_count==class_count;
 wire valid_result=result_flags==3 && class_id<class_count && class_count>=2 && class_count<=32;
 wire can_reply=!response_valid;
 assign command_ready=can_reply && pose_query_wait==0 && (!committing || opcode==16'h0300);
 assign event_ready=can_reply && !command_valid && !committing && pose_query_wait==0;
 assign query_pose=arguments[3:0];
 reg [15:0] summary_range;
 always @*begin
  row=0;if(class_id<32)row=expand_row(active_bank?map1[class_id[4:0]]:map0[class_id[4:0]],class_id[4:0]);
  summary_range=ranges[15:0];
  for(integer n=0;n<8;n=n+1)if(ranges[n*16+:16]!=ranges[15:0] || custom[n])summary_range=16'hffff;
 end
 task reply;input [15:0] s;begin
  response_valid<=1;response_type<=8'h51;response_id<=request_id;response_opcode<=opcode;
  response_status<=s;response_length<=0;response_data<=0;
  json_slot<=7;
 end endtask
 task change_page;input [2:0] p;begin
  page<=p;revision<=revision+1'b1;
  for(integer n=0;n<8;n=n+1)begin
   ranges[n*16+:16]<=range_value(range_pages[p*8+n]);gains[n*16+:16]<=gain_pages[p*8+n];custom[n]<=custom_pages[p*8+n];
  end
 end endtask
 task issue;input [2:0] op;input [15:0] arg;input [15:0] id;begin
  req_valid<=1;req_op<=op;req_arg<=arg;req_id<=id;
 end endtask
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   response_valid<=0;response_type<=0;response_id<=0;response_opcode<=0;response_status<=0;response_length<=0;response_data<=0;
   json_slot<=7;json_words0<=0;json_words1<=0;json_loads<=0;json_ages<=0;
   json_online<=0;json_torque<=0;json_valid<=0;json_torque_valid<=0;json_custom<=0;json_faults<=0;
   req_valid<=0;req_op<=0;req_id<=0;req_arg<=0;
   page<=0;freeze<=0;armed<=0;teach_mode<=0;teach_pending<=0;fault_stop_latched<=0;fault_stop_needed<=0;revision<=0;
   ranges<={8{16'd1200}};gains<={8{16'd256}};custom<=0;custom_pages<=0;
   active_bank<=0;mapping_valid<=0;stage_started<=0;active_generation<=0;active_count<=0;
   stage_generation<=0;stage_count<=0;stage_revision<=0;map_revision<=0;written<=0;
   committing<=0;crc_index<=0;crc<=0;expected_crc<=0;commit_id<=0;
   normal_pending<=0;normal_id<=0;normal_opcode<=0;stop_pending<=0;stop_id<=0;stop_opcode<=0;
   pose_query_wait<=0;pose_query_id<=0;
   result_age<=FRESH_CLKS;result_seen<=0;trigger_ready<=0;last_generation<=0;last_epoch<=0;
   last_class<=16'hffff;last_class_count<=0;last_window<=0;stable_windows<=0;event_age<=MIN_EVENT_CLKS;
   auto_count<=0;auto_rejects<=0;
   for(i=0;i<32;i=i+1)begin range_pages[i]<=i<8?5:i<16?3:2;gain_pages[i]<=256;end
  end else begin
   if(response_valid && response_ready)response_valid<=0;
   if(pose_query_wait!=0)begin
    pose_query_wait<=pose_query_wait-1'b1;
    if(pose_query_wait==1)begin
     response_valid<=1;response_type<=8'h51;response_id<=pose_query_id;response_opcode<=16'h030a;
     response_status<=0;response_length<=13;json_slot<=7;response_data<=
       {{7'd0,flash_initialized},{7'd0,flash_available},
        6'd0,query_positions[49:40],6'd0,query_positions[39:30],6'd0,query_positions[29:20],
        6'd0,query_positions[19:10],6'd0,query_positions[9:0],{7'd0,query_valid}};
    end
   end
   if(req_valid && req_ready)req_valid<=0;
   if(result_age<FRESH_CLKS)result_age<=result_age+1'b1;
   if(event_age<MIN_EVENT_CLKS)event_age<=event_age+1'b1;
   if(motion_state!=1 && motion_state!=2)fault_stop_latched<=0;
   // A model/link revocation can coincide with acceptance of a queued start.
   // Remember it until the bus request is consumed, then send one priority STOP.
   if(fault_stop_needed && !req_valid && !stop_pending)begin
    issue(3,0,16'hfffe);fault_stop_needed<=0;fault_stop_latched<=1;
   end
   if(link_fault || stale || (result_valid && !valid_result) || result_age==FRESH_CLKS-1 ||
      (result_seen && (generation!=last_generation || epoch!=last_epoch || class_count!=last_class_count)))begin
    armed<=0;trigger_ready<=0;stable_windows<=0;
    if((motion_state==1 || motion_state==2) && !fault_stop_latched && !req_valid && !stop_pending)begin
     issue(3,0,16'hfffe);fault_stop_latched<=1;
    end
    if(req_valid && (req_op==2 || req_op==4))fault_stop_needed<=1;
   end
   if(result_valid)begin
    result_age<=0;result_seen<=valid_result;last_generation<=generation;last_epoch<=epoch;last_class_count<=class_count;
    if(!valid_result)begin armed<=0;trigger_ready<=0;stable_windows<=0;end
    else if(!result_seen || generation!=last_generation || epoch!=last_epoch || class_count!=last_class_count)begin
     armed<=0;trigger_ready<=0;stable_windows<=0;last_class<=class_id;last_window<=window_seq;
    end else if(window_seq!=last_window)begin
     last_window<=window_seq;last_class<=class_id;
     if(class_id==0)begin trigger_ready<=1;stable_windows<=0;end
     else if(class_id!=last_class)stable_windows<=1;
     else if(stable_windows<2)stable_windows<=stable_windows+1'b1;
     // Two distinct stable windows, >=200ms between events, rest re-arms.
     if(class_id!=0 && class_id==last_class && stable_windows>=1 && trigger_ready && event_age>=MIN_EVENT_CLKS)begin
      trigger_ready<=0;event_age<=0;
      if(model_matches && row[31:24]==1)begin
       if(row[23:16]>=3 && row[23:16]<=6)begin
        if(display_enabled)begin
         if(row[23:16]==3)change_page(page==0?3:page-1'b1);
         else if(row[23:16]==4)change_page(page==3?0:page+1'b1);
         else begin
          for(i=0;i<8;i=i+1)begin
           ranges[i*16+:16]<=range_value(zoom_code(range_pages[page*8+i],row[23:16]==5));
           range_pages[page*8+i]<=zoom_code(range_pages[page*8+i],row[23:16]==5);
           custom_pages[page*8+i]<=0;
          end
          custom<=0;revision<=revision+1'b1;
         end
         auto_count<=auto_count+1'b1;
        end else auto_rejects<=auto_rejects+1'b1;
       end else if(row[23:16]==1 || row[23:16]==2)begin
        if(armed && !teach_mode && !teach_pending && servo_idle && !req_valid && !normal_pending &&
           online==31 && (row[23:16]!=1 || pose_valid[row[35:32]]) &&
           (row[23:16]!=2 || COLLISION_SUPPORTED))begin
         issue(row[23:16]==1?2:4,row[47:32],16'hffff);auto_count<=auto_count+1'b1;
        end else auto_rejects<=auto_rejects+1'b1;
       end
      end else auto_rejects<=auto_rejects+1'b1;
     end
    end
   end
   if(event_valid && event_ready)begin
    if(normal_pending && event_id==normal_id)begin
     response_valid<=1;response_type<=8'h52;response_id<=normal_id;response_opcode<=normal_opcode;
     response_status<=event_status;response_length<=0;response_data<=0;normal_pending<=0;
     json_slot<=7;
    end else if(stop_pending && event_id==stop_id)begin
     response_valid<=1;response_type<=8'h52;response_id<=stop_id;response_opcode<=stop_opcode;
     response_status<=event_status;response_length<=0;response_data<=0;stop_pending<=0;
     json_slot<=7;
     if(teach_pending)begin teach_pending<=0;teach_mode<=event_status==0;end
    end
   end
   if(committing)begin
    crc<=next_crc;crc_index<=crc_index+1'b1;
    if({1'b0,crc_index}+9'd1=={stage_count[5:0],3'd0})begin
     committing<=0;response_valid<=1;response_type<=8'h51;response_id<=commit_id;
     response_opcode<=16'h0602;response_length<=0;response_data<=0;
     if((next_crc^32'hffffffff)==expected_crc && generation==stage_generation && class_count==stage_count)begin
      active_bank<=!active_bank;mapping_valid<=1;active_generation<=stage_generation;active_count<=stage_count;
      map_revision<=stage_revision;stage_started<=0;armed<=0;trigger_ready<=0;response_status<=0;
     end else response_status<=8;
    end
   end
   if(command_valid && command_ready)begin
    reply(0);
    case(opcode)
     16'h0001:begin
      if(data_length!=0)reply(3);
      else begin
       // Generated constants are byte-packed little-endian, never empty JSON.
       json_slot<=3'd0;response_length<=10'd457;

      end
     end
     16'h0002:begin
      if(data_length>1 || (data_length==1 && arguments[7:0]>4))reply(3);
      else begin
       if(data_length==0 || arguments[7:0]==0)begin
json_slot<=3'd1;response_length<=10'd395;
json_words0<=positions;json_words1<=speeds;json_loads<=loads;json_ages<=feedback_age_ms;
json_online<=online;json_torque<=torque;json_valid<=feedback_valid_mask;json_faults<=fault_flags;json_torque_valid<=torque_valid_mask;
end else begin
case(arguments[2:0])
3'd1:begin
json_slot<=3'd2;response_length<=10'd259;
end
3'd2:begin
json_slot<=3'd3;response_length<=10'd247;
end
3'd3:begin
json_slot<=3'd4;response_length<=10'd255;
end
3'd4:begin
json_slot<=3'd5;response_length<=10'd255;
end
default:reply(3);
endcase
for(integer q=0;q<8;q=q+1)begin json_words0[q*16+:16]<=range_value(range_pages[{json_query_page,q[2:0]}]);json_words1[q*16+:16]<=gain_pages[{json_query_page,q[2:0]}];end
json_custom<=custom_pages[{json_query_page,3'd0}+:8];
end

      end
     end
     16'h0200:if(data_length!=1 || arguments[7:0]>3)reply(3);else change_page(arguments[2:0]);
     16'h0201:if(data_length!=1 || (arguments[7:0]!=1 && arguments[7:0]!=255))reply(3);
      else change_page(arguments[7:0]==1?(page==3?0:page+1'b1):(page==0?3:page-1'b1));
     16'h0202:if(data_length!=1 || arguments[7:0]>1)reply(3);
      else begin freeze<=arguments[0];if(freeze && !arguments[0])revision<=revision+1'b1;end
     16'h0203,16'h0204:begin
      if(data_length!=4 || arguments[7:0]>3 || (arguments[15:8]>7 && arguments[15:8]!=255) ||
       (opcode==16'h0204 ? !legal_range(arguments[31:16]) : (arguments[31:16]==0 || arguments[31:16]>4096)))reply(3);
      else begin
       for(i=0;i<8;i=i+1)if(arguments[15:8]==255 || arguments[15:8]==i)begin
        custom_pages[arguments[1:0]*8+i]<=opcode==16'h0203;
        if(opcode==16'h0204)range_pages[arguments[1:0]*8+i]<=range_code(arguments[31:16]);
        else gain_pages[arguments[1:0]*8+i]<=arguments[28:16];
        if(page==arguments[2:0])begin
         custom[i]<=opcode==16'h0203;
         if(opcode==16'h0204)ranges[i*16+:16]<=arguments[31:16];else gains[i*16+:16]<=arguments[31:16];
        end
       end
       if(page==arguments[2:0])revision<=revision+1'b1;
      end
     end
     16'h0600:begin
      if(data_length!=8 || arguments[31:16]<2 || arguments[31:16]>32)reply(3);
      else if(arguments[15:0]!=generation || arguments[31:16]!=class_count || !result_seen)reply(8);
      else begin stage_generation<=arguments[15:0];stage_count<=arguments[31:16];stage_revision<=arguments[63:32];
       written<=0;stage_started<=1;end
     end
     16'h0601:begin
      if(!stage_started)reply(5);
      else if(data_length!=8 || !legal_row(arguments[63:0]))reply(3);
      else begin
       if(active_bank)map0[arguments[4:0]]<={arguments[35:32],arguments[24],arguments[18:16]};
       else map1[arguments[4:0]]<={arguments[35:32],arguments[24],arguments[18:16]};
       written[arguments[4:0]]<=1;
      end
     end
     16'h0602:begin
      if(data_length!=8)reply(3);
      else if(!servo_idle || req_valid || normal_pending)reply(4);
      else if(!stage_started || written!=row_mask || arguments[31:0]!=stage_revision ||
       generation!=stage_generation || class_count!=stage_count)reply(8);
      else begin committing<=1;crc_index<=0;crc<=32'hffffffff;expected_crc<=arguments[63:32];
       commit_id<=request_id;response_valid<=0;end
     end
     16'h0603:begin
      if(data_length!=2 || arguments[15:0]>=active_count)reply(3);
      else if(!mapping_valid)reply(5);
      else begin response_data<=expand_row(active_bank?map1[arguments[4:0]]:map0[arguments[4:0]],arguments[4:0]);response_length<=8;end
     end
     16'h0604:begin
      if(data_length!=0)reply(3);
      else begin response_length<=28;response_data<=
       {fault_flags,16'd0,summary_range,{5'd0,page},{7'd0,flash_ready},{7'd0,pose_valid},
        {3'd0,retired_mask},{3'd0,active_mask},motion_state,{7'd0,model_matches},map_revision,
        active_count,active_generation,{7'd0,teach_mode},{7'd0,armed},{7'd0,display_enabled},8'd1};end
     end
     16'h0300,16'h0605:begin
      if((opcode==16'h0300 && data_length!=0) ||
         (opcode==16'h0605 && (data_length!=1 || arguments[7:0]>1)))reply(3);
      else if(opcode==16'h0605 && !arguments[0])begin
       if(stop_pending || !servo_idle)reply(4);else begin teach_mode<=0;armed<=0;trigger_ready<=0;end
      end else if(stop_pending)reply(4);
      else begin
       committing<=0;armed<=0;trigger_ready<=0;teach_mode<=0;teach_pending<=opcode==16'h0605;
       stop_pending<=1;stop_id<=request_id;stop_opcode<=opcode;issue(3,0,request_id);reply(1);
      end
     end
     16'h0301:begin
      if(data_length!=2)reply(3);
      else if(!model_matches || arguments[15:0]!=generation || !valid_result || !result_seen ||
        stale || result_age>=FRESH_CLKS || online!=31 || !servo_idle || teach_mode || teach_pending)reply(5);
      else begin armed<=1;trigger_ready<=0;end
     end
     16'h0302:if(data_length!=0)reply(3);else begin armed<=0;trigger_ready<=0;end
     16'h0304,16'h0305,16'h0606:begin
      if(opcode==16'h0606 && !COLLISION_SUPPORTED)reply(6);
      else if((opcode==16'h0606 && data_length!=0) || (opcode!=16'h0606 && (data_length!=2 || arguments[15:0]>=9)))reply(3);
      else if(normal_pending || stop_pending || req_valid || !servo_idle)reply(4);
      else if(online!=31 || (opcode==16'h0304 && (!teach_mode || !flash_ready || torque!=0)) ||
       (opcode==16'h0305 && (!pose_valid[arguments[3:0]] || teach_mode)) ||
       (opcode==16'h0606 && (!armed || teach_mode)))reply(5);
      else begin normal_pending<=1;normal_id<=request_id;normal_opcode<=opcode;
       issue(opcode==16'h0304 ? 1 : opcode==16'h0305 ? 2 : 4,arguments[15:0],request_id);reply(1);end
     end
     16'h030a:begin
      if(data_length!=2 || arguments[15:0]>=9)reply(3);
      else begin response_valid<=0;pose_query_wait<=2;pose_query_id<=request_id;end
     end
     default:reply(6);
    endcase
   end
  end
 end
endmodule
`default_nettype wire

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

`timescale 1ns/1ps
`default_nettype none
// SPI mode-0 data is stable during the low half-period before its rising
// sample edge. Capture halfway between the 24MHz controller edges, then
// consume it on the next rising edge; the SPI bit/byte schedule is unchanged.
module b_tasks_flash_input(input wire clk,miso,output reg captured);
 always @(negedge clk)captured<=miso;
endmodule
`default_nettype wire

