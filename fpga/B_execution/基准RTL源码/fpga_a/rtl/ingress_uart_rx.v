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
