`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;
reg rst=0,b_rx=1;wire pc_tx;wire [7:0] observed;wire observed_valid;
v2_pc apc(.clk(clk),.rst_n(rst),.rx(1'b1),.tx(pc_tx),.b_rx(b_rx),
 .bridge_diagnostic(256'd0),.stream_valid(1'b0),.stream_type(8'd0),.stream_length(11'd0),
 .stream_payload_valid(1'b0),.stream_data(8'd0),.pipeline_idle(1'b1),.timestamp(32'd123),
 .accepted(32'd0),.wave_drops(32'd0),.results(32'd0),.wireless_bad(32'd0),
 .diagnostic_snapshot(512'd0),.forward_ready(1'b1),.forward_take(1'b1),.replay_ready(1'b1));
ingress_uart_rx monitor(clk,rst,pc_tx,observed,observed_valid,/*err*/,/*busy*/);
reg [7:0] input_bytes[0:1305-1];integer capture_file,i,j,p,first=0;integer bounds[0:6];
always @(posedge clk) if(observed_valid) $fdisplay(capture_file,"%02x",observed);
initial begin
 capture_file=$fopen("pc.hex","w");$readmemh("input.hex",input_bytes);
 bounds[0]=478;bounds[1]=502;bounds[2]=526;bounds[3]=578;bounds[4]=615;bounds[5]=1034;bounds[6]=1305;
 repeat(10)@(negedge clk);rst=1;repeat(10)@(negedge clk);
 for(p=0;p<7;p=p+1) begin
  for(i=first;i<bounds[p];i=i+1) begin
   b_rx=0;repeat(26)@(negedge clk);
   for(j=0;j<8;j=j+1) begin b_rx=input_bytes[i][j];repeat(26)@(negedge clk);end
   b_rx=1;repeat(26)@(negedge clk);
  end
  first=bounds[p];repeat(160000)@(negedge clk);
 end
 if(apc.ret.error_count!=0)$fatal(1,"B capture rejected");
 $fclose(capture_file);$display("B_CAPTURE_REPLAY_PASS");$finish;
end
initial begin #30000000;$fatal(1,"timeout");end
endmodule
