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
