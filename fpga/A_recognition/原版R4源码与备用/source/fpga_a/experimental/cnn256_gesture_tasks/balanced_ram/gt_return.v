`default_nettype none
// Two complete CRC-checked packet banks. A full bank is never overwritten.
// UART cannot be backpressured; full/invalid/timeout frames are counted once.
module gt_return #(parameter integer BIT_CYCLES=26)(
 input wire clk,rst_n,rx,
 output wire frame_valid,input wire frame_ready,output wire [7:0] frame_type,
 output wire [10:0] frame_length,output wire payload_valid,input wire payload_ready,
 output wire [7:0] payload_data,output wire [31:0] errors,
 output wire event_valid,output wire [15:0] event_id,event_opcode,output wire event_final
);
 wire [7:0] rb;wire bv,fe;
 ingress_uart_rx #(.CLKS_PER_BIT(BIT_CYCLES)) uart(clk,rst_n,rx,rb,bv,fe,/*busy*/);
 (* ram_style="bram" *) reg [7:0] memory[0:2047];
 reg [7:0] q;reg [3:0] state;
 reg [10:0] length,index,out_index,out_left;
 reg [7:0] kind,crc_low;
 reg [79:0] header;
 reg [15:0] crc,error_count;
 reg [17:0] idle_count;
 reg write_bank,read_bank,discard,active;
 reg [1:0] occupied;
 reg [7:0] types[0:1];reg [10:0] lengths[0:1];
 wire [15:0] crc_next;
 link_crc16_ccitt step(crc,rb,crc_next);
 wire header_ok=(kind==8'h51 || kind==8'h52) && length>=10 && header[7:0]==1 && header[15:8]==2 && header[79:64]==length-10;
 wire commit=bv && state==15 && {rb,crc_low}==crc && header_ok && !discard;
 assign event_valid=commit;assign event_id=header[31:16];assign event_opcode=header[47:32];
 assign event_final=kind==8'h52 || header[63:48]!=1;
 assign errors={16'b0,error_count};
 assign frame_valid=occupied[read_bank] && !active;
 assign frame_type=types[read_bank];assign frame_length=lengths[read_bank];
 assign payload_valid=active;assign payload_data=q;
 wire [9:0] prefetch=(active && payload_ready && out_left>1) ? out_index[9:0]+1'b1 : out_index[9:0];
 always @(posedge clk) begin
  q<=memory[{read_bank,prefetch}];
  if(bv && state==13 && !discard) memory[{write_bank,index[9:0]}]<=rb;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   state<=0;length<=0;index<=0;kind<=0;crc_low<=0;header<=0;crc<=16'hffff;
   error_count<=0;idle_count<=0;write_bank<=0;read_bank<=0;discard<=0;active<=0;
   occupied<=0;out_index<=0;out_left<=0;
  end else begin
   if(frame_valid && frame_ready) begin active<=1;out_index<=0;out_left<=lengths[read_bank];end
   if(active && payload_ready) begin
    out_index<=out_index+1'b1;out_left<=out_left-1'b1;
    if(out_left==1) begin occupied[read_bank]<=0;read_bank<=!read_bank;active<=0;out_index<=0;end
   end
   if(fe || (state!=0 && !bv && idle_count==239999)) begin
    state<=0;idle_count<=0;if(error_count!=65535) error_count<=error_count+1'b1;
   end else if(bv) begin
    idle_count<=0;
    if(state>=2 && state<=13) crc<=crc_next;
    case(state)
     0:if(rb==8'ha5) state<=1;
     1:if(rb==8'h5a) begin state<=2;crc<=16'hffff;discard<=occupied[write_bank];header<=0;end
       else if(rb!=8'ha5) state<=0;
     2:if(rb==1) state<=3;else begin state<=0;if(error_count!=65535) error_count<=error_count+1'b1;end
     3:begin kind<=rb;state<=4;end
     4,5:state<=state+1'b1;
     6:begin length[7:0]<=rb;state<=7;end
     7:if({rb,length[7:0]}>1024) begin state<=0;if(error_count!=65535) error_count<=error_count+1'b1;end
       else begin length[10:8]<=rb[2:0];state<=8;end
     8,9,10:state<=state+1'b1;
     11:begin index<=0;state<=length==0 ? 14 : 13;end
     13:begin
      if(index<10) header[index*8 +:8]<=rb;
      index<=index+1'b1;if(index==length-1) state<=14;
     end
     14:begin crc_low<=rb;state<=15;end
     15:begin
      state<=0;
      if(commit) begin occupied[write_bank]<=1;types[write_bank]<=kind;lengths[write_bank]<=length;write_bank<=!write_bank;end
      else if(error_count!=65535) error_count<=error_count+1'b1;
     end
     default:state<=0;
    endcase
   end else if(state!=0) idle_count<=idle_count+1'b1;
  end
 end
endmodule
`default_nettype wire
