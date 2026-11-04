`timescale 1ns/1ps

module b_tasks_engine #(
    
    parameter integer RESPONSE_TIMEOUT_CLKS = 48000
) (
    input  wire        clk,
    input  wire        rst_n,
    input wire [7:0] SERVO_ID,
    input wire [15:0] command_speed,
    output reg [7:0] torque_value,
    output reg [391:0] configuration,
    input  wire        command_valid,
    input  wire [2:0]  command,
    input  wire [9:0]  target_position,
    input  wire        servo_rx,
    output wire        servo_tx,
    output reg         tx_enable,
    output reg         busy,
    output reg         response_valid,
    output reg  [2:0]  response_command,
    output reg  [3:0]  result_code,
    output reg  [7:0]  servo_error,
    // Position from the response that raised response_valid.  This is
    // separate from present_position because both registers are committed
    // on the same clock edge.
    output reg  [15:0] response_position,
    output reg  [15:0] present_position,
    output reg  [15:0] present_speed,
    output reg  [15:0] present_load,
    output reg  [15:0] present_current,
    output reg  [7:0]  present_voltage,
    output reg  [7:0]  present_temperature,
    output reg         online
);

    localparam [2:0] CMD_PING       = 3'd0;
    localparam [2:0] CMD_READ_STATE = 3'd1;
    localparam [2:0] CMD_TORQUE_OFF = 3'd2;
    localparam [2:0] CMD_TORQUE_ON  = 3'd3;
    localparam [2:0] CMD_MOVE       = 3'd4;
    localparam [2:0] CMD_HOLD_HIGH  = 3'd5;
    localparam [2:0] CMD_HOLD_LOW   = 3'd6;
    localparam [2:0] CMD_SLOW_MOVE  = 3'd7;

    localparam [2:0] ST_IDLE = 3'd0;
    localparam [2:0] ST_TX   = 3'd1;
    localparam [2:0] ST_RX   = 3'd2;

    localparam [2:0] PS_HEAD1 = 3'd0;
    localparam [2:0] PS_HEAD2 = 3'd1;
    localparam [2:0] PS_ID    = 3'd2;
    localparam [2:0] PS_LEN   = 3'd3;
    localparam [2:0] PS_ERROR = 3'd4;
    localparam [2:0] PS_PARAM = 3'd5;
    localparam [2:0] PS_CHECK = 3'd6;

    reg [2:0] state;
    reg [2:0] parser_state;
    reg [2:0] command_latched;
    reg [9:0] target_latched;reg [15:0] speed_latched;reg [7:0] torque_temp;
    reg [3:0] tx_length;
    reg [3:0] tx_index;
    reg [7:0] selected_tx_byte;
    reg tx_issued;
    reg tx_start;
    reg [7:0] tx_data;
    wire tx_busy;
    wire tx_done;

    reg [20:0] timeout_count;
    reg [7:0] response_length;
    reg [7:0] checksum_sum;
    reg [7:0] parameter_index;
    reg [7:0] received_byte_count;
    reg [15:0] temp_position;
    reg [15:0] temp_speed;
    reg [15:0] temp_load;
    reg [15:0] temp_current;
    reg [7:0] temp_voltage;
    reg [7:0] temp_temperature;

    wire [7:0] rx_data;
    wire rx_valid;
    wire rx_busy;
    wire rx_framing_error;
    wire rx_enable;

    assign rx_enable = (state == ST_RX);

    always @(*) begin
        selected_tx_byte = 8'hff;
        if (tx_index == 0)
            selected_tx_byte = 8'hff;
        else if (tx_index == 1)
            selected_tx_byte = 8'hff;
        else if (tx_index == 2)
            selected_tx_byte = SERVO_ID;
        else begin
            case (command_latched)
                CMD_PING: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd2;
                        4: selected_tx_byte = 8'h01;
                        5: selected_tx_byte = ~(SERVO_ID + 8'd2 + 8'h01);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_READ_STATE: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h02;
                        5: selected_tx_byte = 8'd56;
                        6: selected_tx_byte = 8'd15;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h02 + 8'd56 + 8'd15);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_TORQUE_OFF: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h03;
                        5: selected_tx_byte = 8'd40;
                        6: selected_tx_byte = 8'd0;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h03 + 8'd40);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_TORQUE_ON: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd4;
                        4: selected_tx_byte = 8'h03;
                        5: selected_tx_byte = 8'd40;
                        6: selected_tx_byte = 8'd1;
                        7: selected_tx_byte = ~(SERVO_ID + 8'd4 + 8'h03 + 8'd40 + 8'd1);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_MOVE, CMD_SLOW_MOVE: begin
                    case (tx_index)
                        3:  selected_tx_byte = 8'd9;
                        4:  selected_tx_byte = 8'h03;
                        5:  selected_tx_byte = 8'd42;
                        6:  selected_tx_byte = {6'd0, target_latched[9:8]};
                        7:  selected_tx_byte = target_latched[7:0];
                        8:  selected_tx_byte = 8'd0;
                        9:  selected_tx_byte = 8'd0;
                        10: selected_tx_byte = speed_latched[15:8];
                        11: selected_tx_byte = speed_latched[7:0];
                        12: selected_tx_byte = ~(SERVO_ID + 8'd9 + 8'h03 + 8'd42 +
                                                  {6'd0, target_latched[9:8]} +
                                                  target_latched[7:0] +
                                                  speed_latched[15:8]+speed_latched[7:0]);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end

                CMD_HOLD_HIGH, CMD_HOLD_LOW: begin
                    case(tx_index)
                     3:selected_tx_byte=4;
                     4:selected_tx_byte=2;
                     5:selected_tx_byte=command_latched==CMD_HOLD_HIGH?0:40;
                     6:selected_tx_byte=command_latched==CMD_HOLD_HIGH?49:1;
                     7:selected_tx_byte=~(SERVO_ID+8'd4+8'd2+(command_latched==CMD_HOLD_LOW?8'd41:8'd49));
                     default:selected_tx_byte=8'hff;
                    endcase
                end
                default: begin
                    case (tx_index)
                        3: selected_tx_byte = 8'd2;
                        4: selected_tx_byte = 8'h01;
                        5: selected_tx_byte = ~(SERVO_ID + 8'd2 + 8'h01);
                        default: selected_tx_byte = 8'hff;
                    endcase
                end
            endcase
        end
    end

    uart_tx_byte #(
        .CLKS_PER_BIT(24)
    ) servo_uart_tx (
        .clk   (clk),
        .rst_n (rst_n),
        .start (tx_start),
        .data  (tx_data),
        .tx    (servo_tx),
        .busy  (tx_busy),
        .done  (tx_done)
    );

    uart_rx_byte #(
        .CLKS_PER_BIT(24)
    ) servo_uart_rx (
        .clk           (clk),
        .rst_n         (rst_n),
        .enable        (rx_enable),
        .rx            (servo_rx),
        .data          (rx_data),
        .valid         (rx_valid),
        .busy          (rx_busy),
        .framing_error (rx_framing_error)
    );

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state               <= ST_IDLE;
            parser_state        <= PS_HEAD1;
            command_latched     <= CMD_PING;
            target_latched      <= 10'd512;
            tx_length           <= 4'd0;
            tx_index            <= 4'd0;
            tx_issued           <= 1'b0;
            tx_start            <= 1'b0;
            tx_data             <= 8'hff;
            tx_enable           <= 1'b0;
            busy                <= 1'b0;
            response_valid      <= 1'b0;
            response_command    <= CMD_PING;
            result_code         <= 4'd0;
            servo_error         <= 8'd0;
            response_position   <= 16'd0;
            present_position    <= 16'd0;
            present_speed       <= 16'd0;
            present_load        <= 16'd0;
            present_current     <= 16'd0;
            present_voltage     <= 8'd0;
            present_temperature <= 8'd0;
            online              <= 1'b0;
            timeout_count       <= 21'd0;
            response_length     <= 8'd0;
            checksum_sum        <= 8'd0;
            parameter_index     <= 8'd0;
            received_byte_count <= 8'd0;
            temp_position       <= 16'd0;
            temp_speed          <= 16'd0;
            temp_load           <= 16'd0;
            temp_current        <= 16'd0;
            temp_voltage        <= 8'd0;
            temp_temperature    <= 8'd0;
        end else begin
            tx_start       <= 1'b0;
            response_valid <= 1'b0;

            case (state)
                ST_IDLE: begin
                    busy      <= 1'b0;
                    tx_enable <= 1'b0;
                    tx_issued <= 1'b0;

                    if (command_valid) begin
                        busy              <= 1'b1;
                        tx_enable         <= 1'b1;
                        command_latched   <= command;
                        target_latched    <= target_position;speed_latched<=command_speed;
                        response_command  <= command;
                        result_code       <= 4'd0;
                        servo_error       <= 8'd0;
                        tx_index          <= 4'd0;
                        tx_issued         <= 1'b0;
                        timeout_count     <= 21'd0;
                        parser_state      <= PS_HEAD1;
                        parameter_index   <= 8'd0;
                        received_byte_count <= 8'd0;
                        state             <= ST_TX;

                        case (command)
                            CMD_PING:       tx_length <= 4'd6;
                            CMD_READ_STATE: tx_length <= 4'd8;
                            CMD_TORQUE_OFF: tx_length <= 4'd8;
                            CMD_TORQUE_ON:  tx_length <= 4'd8;
                            CMD_MOVE, CMD_SLOW_MOVE: tx_length <= 4'd13;
                            CMD_HOLD_HIGH:  tx_length <= 4'd8;
                            CMD_HOLD_LOW:   tx_length <= 4'd8;
                            default:        tx_length <= 4'd6;
                        endcase
                    end
                end

                ST_TX: begin
                    if (!tx_issued && !tx_busy) begin
                        tx_data   <= selected_tx_byte;
                        tx_start  <= 1'b1;
                        tx_issued <= 1'b1;
                    end

                    if (tx_done && tx_issued) begin
                        tx_issued <= 1'b0;
                        if (tx_index == tx_length - 1'b1) begin
                            tx_enable     <= 1'b0;
                            timeout_count <= 21'd0;
                            parser_state  <= PS_HEAD1;
                            state <= ST_RX;
                        end else begin
                            tx_index <= tx_index + 1'b1;
                        end
                    end
                end

                ST_RX: begin
                    if (timeout_count == RESPONSE_TIMEOUT_CLKS - 1) begin
                        state            <= ST_IDLE;
                        busy             <= 1'b0;
                        response_valid   <= 1'b1;
                        response_command <= command_latched;
                        result_code      <= 4'd1;
                        servo_error      <= received_byte_count;
                        online           <= 1'b0;
                    end else begin
                        timeout_count <= timeout_count + 1'b1;
                    end

                    if (rx_framing_error) begin
                        state            <= ST_IDLE;
                        busy             <= 1'b0;
                        response_valid   <= 1'b1;
                        response_command <= command_latched;
                        result_code      <= 4'd3;
                    end else if (rx_valid) begin
                        if (received_byte_count != 8'hff)
                            received_byte_count <= received_byte_count + 1'b1;
                        case (parser_state)
                            PS_HEAD1: begin
                                if (rx_data == 8'hff)
                                    parser_state <= PS_HEAD2;
                            end

                            PS_HEAD2: begin
                                if (rx_data == 8'hff)
                                    parser_state <= PS_ID;
                                else
                                    parser_state <= PS_HEAD1;
                            end

                            PS_ID: begin
                                if (rx_data == SERVO_ID) begin
                                    checksum_sum <= rx_data;
                                    parser_state <= PS_LEN;
                                end else begin
                                    parser_state <= PS_HEAD1;
                                end
                            end

                            PS_LEN: begin
                                if ((rx_data >= 8'd2) && (rx_data <= 8'd51)) begin
                                    response_length <= rx_data;
                                    checksum_sum    <= checksum_sum + rx_data;
                                    parser_state    <= PS_ERROR;
                                end else begin
                                    state            <= ST_IDLE;
                                    busy             <= 1'b0;
                                    response_valid   <= 1'b1;
                                    response_command <= command_latched;
                                    result_code      <= 4'd3;
                                end
                            end

                            PS_ERROR: begin
                                servo_error  <= rx_data;
                                checksum_sum <= checksum_sum + rx_data;
                                if (response_length == 8'd2)
                                    parser_state <= PS_CHECK;
                                else begin
                                    parameter_index <= 8'd0;
                                    parser_state    <= PS_PARAM;
                                end
                            end

                            PS_PARAM: begin
                                checksum_sum <= checksum_sum + rx_data;
                                if(command_latched==CMD_HOLD_HIGH)configuration[parameter_index*8+:8]<=rx_data;
                                if(command_latched==CMD_HOLD_LOW)torque_temp<=rx_data;
                                case (parameter_index)
                                    0: temp_position[15:8] <= rx_data;
                                    1: temp_position[7:0]  <= rx_data;
                                    2: temp_speed[15:8]    <= rx_data;
                                    3: temp_speed[7:0]     <= rx_data;
                                    4: temp_load[15:8]     <= rx_data;
                                    5: temp_load[7:0]      <= rx_data;
                                    6: temp_voltage         <= rx_data;
                                    7: temp_temperature     <= rx_data;
                                    // SCSCL End=1: address 69 is the high byte,
                                    // address 70 the low byte; bit 15 is sign.
                                    13: temp_current[15:8]   <= rx_data;
                                    14: temp_current[7:0]    <= rx_data;
                                    default: temp_temperature <= temp_temperature;
                                endcase

                                if (parameter_index == response_length - 8'd3)
                                    parser_state <= PS_CHECK;
                                else
                                    parameter_index <= parameter_index + 1'b1;
                            end

                            PS_CHECK: begin
                                state            <= ST_IDLE;
                                busy             <= 1'b0;
                                response_valid   <= 1'b1;
                                response_command <= command_latched;

                                if (rx_data != ~checksum_sum) begin
                                    result_code <= 4'd2;
                                end else if (((command_latched == CMD_READ_STATE) &&
                                              (response_length != 8'd17)) ||
                                             ((command_latched == CMD_HOLD_HIGH) && response_length!=51) ||
                                             ((command_latched == CMD_HOLD_LOW) && response_length!=3) ||
                                             ((command_latched != CMD_READ_STATE && command_latched != CMD_HOLD_HIGH && command_latched != CMD_HOLD_LOW) && response_length!=2)) begin
                                    result_code <= 4'd3;
                                end else begin
                                    online      <= 1'b1;
                                    result_code <= ((servo_error & 8'hfe) == 0) ? 4'd0 : 4'd4;
                                    
                                    if(command_latched==CMD_HOLD_LOW)torque_value<=torque_temp;
                                    if (command_latched == CMD_READ_STATE) begin
                                        response_position  <= temp_position;
                                        present_position    <= temp_position;
                                        present_speed       <= temp_speed;
                                        present_load        <= temp_load;
                                        present_current     <= temp_current;
                                        present_voltage     <= temp_voltage;
                                        present_temperature <= temp_temperature;
                                    end
                                end
                            end

                            default: parser_state <= PS_HEAD1;
                        endcase
                    end
                end

                default: state <= ST_IDLE;
            endcase
        end
    end

endmodule
