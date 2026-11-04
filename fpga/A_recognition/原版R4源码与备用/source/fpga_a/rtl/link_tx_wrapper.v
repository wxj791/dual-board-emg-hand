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
