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
