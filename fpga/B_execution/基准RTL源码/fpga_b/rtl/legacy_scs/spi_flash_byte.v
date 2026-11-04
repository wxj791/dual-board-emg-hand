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
