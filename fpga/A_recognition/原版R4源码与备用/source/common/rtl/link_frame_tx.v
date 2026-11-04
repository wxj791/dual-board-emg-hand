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
