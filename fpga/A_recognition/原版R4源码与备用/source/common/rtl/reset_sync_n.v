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
