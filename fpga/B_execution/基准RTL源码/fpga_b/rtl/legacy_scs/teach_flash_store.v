`timescale 1ns/1ps

module teach_flash_store (
    input wire clk,
    input wire rst_n,
    input wire save_req,
    input wire [3:0] save_group,
    input wire [49:0] save_positions,
    output reg init_done,
    output reg available,
    output reg load_valid,
    output reg [3:0] load_group,
    output reg [49:0] load_positions,
    output reg save_done,
    output reg save_ok,
    output reg flash_cs_n,
    output wire flash_clk,
    output wire flash_mosi,
    input wire flash_miso
);
    // TD 6.2.1 crashes when this module's state constants share one source
    // with the five packet engines; keep the Flash module separate in the project.
    // Transaction ops: ID=0, READ=1, WREN=2, ERASE=3, STATUS=4, PROGRAM=5.
    // States 0..8 cover startup/erase; 9..17 cover program/verify/SPI transfer.

    reg [4:0] state, return_state;
    reg [2:0] txn_op;
    reg [23:0] txn_addr;
    reg [4:0] txn_index;
    reg [1:0] gap_count;
    reg byte_start;
    reg [7:0] byte_tx;
    wire [7:0] byte_rx;
    wire byte_busy, byte_done;
    reg [7:0] id0, id1, id2, status_byte;
    reg [143:0] record_buf, write_record;
    reg [3:0] scan_group, pending_group;
    reg scan_slot, pending_slot;
    reg [7:0] pending_generation;
    reg candidate_valid, candidate_slot;
    reg [7:0] candidate_generation;
    reg [49:0] candidate_positions;
    reg [8:0] latest_valid;
    reg [8:0] latest_slot;
    reg [71:0] latest_generation;
    reg [15:0] poll_count;

    function [23:0] sector_address;
        input [3:0] group_index;
        input slot_index;
        begin
            sector_address = 24'h1e0000 + {7'd0, group_index, slot_index, 12'd0};
        end
    endfunction

    function [15:0] crc16;
        input [127:0] data;
        reg [15:0] crc;
        reg feedback;
        integer bit_number;
        begin
            crc = 16'hffff;
            for (bit_number = 0; bit_number < 128; bit_number = bit_number + 1) begin
                feedback = crc[15] ^ data[127 - bit_number];
                crc = {crc[14:0], 1'b0};
                if (feedback) crc = crc ^ 16'h1021;
            end
            crc16 = crc;
        end
    endfunction

    function [143:0] make_record;
        input [3:0] group_index;
        input [7:0] generation;
        input [49:0] positions;
        reg [127:0] body;
        begin
            body = {32'h53435335, 8'd1 + {4'd0, group_index}, generation,
                    6'd0, positions[9:0], 6'd0, positions[19:10],
                    6'd0, positions[29:20], 6'd0, positions[39:30],
                    6'd0, positions[49:40]};
            make_record = {body, crc16(body)};
        end
    endfunction

    function [49:0] decode_positions;
        input [79:0] raw;
        begin
            decode_positions = {raw[9:0], raw[25:16], raw[41:32],
                                raw[57:48], raw[73:64]};
        end
    endfunction

    wire valid_record = (record_buf[143:112] == 32'h53435335) &&
                        (record_buf[111:104] == {4'd0, scan_group} + 8'd1) &&
                        (record_buf[15:0] == crc16(record_buf[143:16]));
    wire newer_record = (record_buf[103:96] != candidate_generation) &&
                        ((record_buf[103:96] - candidate_generation) < 8'd128);

    spi_flash_byte byte_engine (
        .clk(clk), .rst_n(rst_n), .start(byte_start), .tx_data(byte_tx),
        .miso(flash_miso), .sclk(flash_clk), .mosi(flash_mosi),
        .busy(byte_busy), .done(byte_done), .rx_data(byte_rx)
    );

    always @(*) begin
        byte_tx = 8'd0;
        case (txn_op)
            3'd0: if (txn_index == 0) byte_tx = 8'h9f;
            3'd2: byte_tx = 8'h06;
            3'd4: if (txn_index == 0) byte_tx = 8'h05;
            3'd1, 3'd3, 3'd5: begin
                case (txn_index)
                    0: byte_tx = txn_op == 3'd1 ? 8'h03 :
                                 txn_op == 3'd3 ? 8'h20 : 8'h02;
                    1: byte_tx = txn_addr[23:16];
                    2: byte_tx = txn_addr[15:8];
                    3: byte_tx = txn_addr[7:0];
                    default: if (txn_op == 3'd5) begin
                        case (txn_index)
                            5'd4: byte_tx = write_record[143:136];
                            5'd5: byte_tx = write_record[135:128];
                            5'd6: byte_tx = write_record[127:120];
                            5'd7: byte_tx = write_record[119:112];
                            5'd8: byte_tx = write_record[111:104];
                            5'd9: byte_tx = write_record[103:96];
                            5'd10: byte_tx = write_record[95:88];
                            5'd11: byte_tx = write_record[87:80];
                            5'd12: byte_tx = write_record[79:72];
                            5'd13: byte_tx = write_record[71:64];
                            5'd14: byte_tx = write_record[63:56];
                            5'd15: byte_tx = write_record[55:48];
                            5'd16: byte_tx = write_record[47:40];
                            5'd17: byte_tx = write_record[39:32];
                            5'd18: byte_tx = write_record[31:24];
                            5'd19: byte_tx = write_record[23:16];
                            5'd20: byte_tx = write_record[15:8];
                            5'd21: byte_tx = write_record[7:0];
                            default: byte_tx = 8'd0;
                        endcase
                    end
                endcase
            end
            default: byte_tx = 8'd0;
        endcase
    end
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= 5'd0;
            return_state <= 5'd1;
            txn_op <= 3'd0;
            txn_addr <= 24'd0;
            txn_index <= 5'd0;
            gap_count <= 2'd0;
            byte_start <= 1'b0;
            flash_cs_n <= 1'b1;
            id0 <= 8'd0; id1 <= 8'd0; id2 <= 8'd0;
            status_byte <= 8'd0;
            record_buf <= 144'd0;
            write_record <= 144'd0;
            scan_group <= 4'd0;
            scan_slot <= 1'b0;
            pending_group <= 4'd0;
            pending_slot <= 1'b0;
            pending_generation <= 8'd0;
            candidate_valid <= 1'b0;
            candidate_slot <= 1'b0;
            candidate_generation <= 8'd0;
            candidate_positions <= 50'd0;
            latest_valid <= 9'd0;
            latest_slot <= 9'd0;
            latest_generation <= 72'd0;
            poll_count <= 16'd0;
            init_done <= 1'b0;
            available <= 1'b0;
            load_valid <= 1'b0;
            load_group <= 4'd0;
            load_positions <= 50'd0;
            save_done <= 1'b0;
            save_ok <= 1'b0;
        end else begin
            byte_start <= 1'b0;
            load_valid <= 1'b0;
            save_done <= 1'b0;
            case (state)
                5'd0: begin
                    txn_op <= 3'd0;
                    return_state <= 5'd1;
                    state <= 5'd14;
                end
                5'd1: begin
                    if ((id0 == 8'h68) && (id1 == 8'h40) && (id2 == 8'h15)) begin
                        available <= 1'b1;
                        scan_group <= 4'd0;
                        scan_slot <= 1'b0;
                        candidate_valid <= 1'b0;
                        txn_op <= 3'd1;
                        txn_addr <= sector_address(4'd0, 1'b0);
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end else begin
                        init_done <= 1'b1;
                        state <= 5'd5;
                    end
                end
                5'd2: begin
                    if (valid_record &&
                        (!candidate_valid || newer_record)) begin
                        candidate_valid <= 1'b1;
                        candidate_slot <= scan_slot;
                        candidate_generation <= record_buf[103:96];
                        candidate_positions <= decode_positions(record_buf[95:16]);
                    end
                    if (!scan_slot) begin
                        scan_slot <= 1'b1;
                        txn_addr <= sector_address(scan_group, 1'b1);
                        txn_op <= 3'd1;
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end else state <= 5'd3;
                end
                5'd3: begin
                    if (candidate_valid) begin
                        latest_valid[scan_group] <= 1'b1;
                        latest_slot[scan_group] <= candidate_slot;
                        latest_generation[scan_group * 8 +: 8] <= candidate_generation;
                        load_valid <= 1'b1;
                        load_group <= scan_group;
                        load_positions <= candidate_positions;
                    end
                    if (scan_group == 4'd8) state <= 5'd4;
                    else begin
                        scan_group <= scan_group + 1'b1;
                        scan_slot <= 1'b0;
                        candidate_valid <= 1'b0;
                        txn_addr <= sector_address(scan_group + 1'b1, 1'b0);
                        txn_op <= 3'd1;
                        return_state <= 5'd2;
                        state <= 5'd14;
                    end
                end
                5'd4: begin
                    init_done <= 1'b1;
                    state <= 5'd5;
                end
                5'd5: begin
                    if (save_req) begin
                        if (available && (save_group <= 4'd8)) begin
                            pending_group <= save_group;
                            pending_slot <= latest_valid[save_group] ? !latest_slot[save_group] : 1'b0;
                            pending_generation <= latest_valid[save_group] ?
                                latest_generation[save_group * 8 +: 8] + 1'b1 : 8'd0;
                            write_record <= make_record(save_group,
                                latest_valid[save_group] ? latest_generation[save_group * 8 +: 8] + 1'b1 : 8'd0,
                                save_positions);
                            txn_addr <= sector_address(save_group,
                                latest_valid[save_group] ? !latest_slot[save_group] : 1'b0);
                            txn_op <= 3'd2;
                            return_state <= 5'd6;
                            state <= 5'd14;
                        end else state <= 5'd13;
                    end
                end
                5'd6: begin
                    txn_op <= 3'd3;
                    return_state <= 5'd7;
                    state <= 5'd14;
                end
                5'd7: begin
                    poll_count <= 16'd0;
                    txn_op <= 3'd4;
                    return_state <= 5'd8;
                    state <= 5'd14;
                end
                5'd8: begin
                    if (status_byte[0]) begin
                        if (poll_count == 16'd60000) state <= 5'd13;
                        else begin
                            poll_count <= poll_count + 1'b1;
                            txn_op <= 3'd4;
                            return_state <= 5'd8;
                            state <= 5'd14;
                        end
                    end else begin
                        txn_op <= 3'd2;
                            return_state <= 5'd9;
                        state <= 5'd14;
                    end
                end
                5'd9: begin
                    txn_op <= 3'd5;
                    return_state <= 5'd10;
                    state <= 5'd14;
                end
                5'd10: begin
                    poll_count <= 16'd0;
                    txn_op <= 3'd4;
                    return_state <= 5'd11;
                    state <= 5'd14;
                end
                5'd11: begin
                    if (status_byte[0]) begin
                        if (poll_count == 16'd60000) state <= 5'd13;
                        else begin
                            poll_count <= poll_count + 1'b1;
                            txn_op <= 3'd4;
                            return_state <= 5'd11;
                            state <= 5'd14;
                        end
                    end else begin
                        txn_op <= 3'd1;
                        return_state <= 5'd12;
                        state <= 5'd14;
                    end
                end
                5'd12: begin
                    if (record_buf == write_record) begin
                        latest_valid[pending_group] <= 1'b1;
                        latest_slot[pending_group] <= pending_slot;
                        latest_generation[pending_group * 8 +: 8] <= pending_generation;
                        save_ok <= 1'b1;
                        save_done <= 1'b1;
                        state <= 5'd5;
                    end else state <= 5'd13;
                end
                5'd13: begin
                    save_ok <= 1'b0;
                    save_done <= 1'b1;
                    state <= 5'd5;
                end
                5'd14: begin
                    flash_cs_n <= 1'b0;
                    txn_index <= 5'd0;
                    state <= 5'd15;
                end
                5'd15: begin
                    if (!byte_busy && !byte_start) begin
                        byte_start <= 1'b1;
                        state <= 5'd16;
                    end
                end
                5'd16: begin
                    if (byte_done) begin
                        if (txn_op == 3'd0) begin
                            case (txn_index)
                                5'd1: id0 <= byte_rx;
                                5'd2: id1 <= byte_rx;
                                5'd3: id2 <= byte_rx;
                                default: begin end
                            endcase
                        end else if ((txn_op == 3'd1) && (txn_index >= 5'd4))
                            record_buf <= {record_buf[135:0], byte_rx};
                        else if ((txn_op == 3'd4) && (txn_index == 5'd1))
                            status_byte <= byte_rx;

                        if (((txn_op == 3'd0 || txn_op == 3'd3) && txn_index == 5'd3) ||
                            (txn_op == 3'd2 && txn_index == 5'd0) ||
                            (txn_op == 3'd4 && txn_index == 5'd1) ||
                            ((txn_op == 3'd1 || txn_op == 3'd5) && txn_index == 5'd21)) begin
                            flash_cs_n <= 1'b1;
                            gap_count <= 2'd0;
                            state <= 5'd17;
                        end else begin
                            txn_index <= txn_index + 1'b1;
                            state <= 5'd15;
                        end
                    end
                end
                5'd17: begin
                    if (gap_count == 2'd3) state <= return_state;
                    else gap_count <= gap_count + 1'b1;
                end
                default: state <= 5'd0;
            endcase
        end
    end
endmodule
