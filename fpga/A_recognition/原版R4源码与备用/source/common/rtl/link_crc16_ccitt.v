`default_nettype none

module link_crc16_ccitt (
    input  wire [15:0] crc_in,
    input  wire [7:0]  data_in,
    output reg  [15:0] crc_out
);
    integer bit_index;
    reg [15:0] work;

    always @* begin
        work = crc_in ^ {data_in, 8'h00};
        for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1) begin
            if (work[15])
                work = {work[14:0], 1'b0} ^ 16'h1021;
            else
                work = {work[14:0], 1'b0};
        end
        crc_out = work;
    end
endmodule

`default_nettype wire
