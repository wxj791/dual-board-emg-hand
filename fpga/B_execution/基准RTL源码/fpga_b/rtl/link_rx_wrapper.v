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
