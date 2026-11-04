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
