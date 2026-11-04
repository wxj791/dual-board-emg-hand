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
