`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;reg rst_n=0,sv=0;reg [127:0] data=0;
reg [31:0] seq=0;reg [15:0] active=7;wire sr,rv;wire [31:0] cc,hc,fc;
fusion_cnn_stream #(.CLASSES(32)) dut(.clk(clk),.rst_n(rst_n),.active_classes(active),.sample_seq(seq),
 .sample_valid(sv),.sample_ready(sr),.sample_mv(data),.result_valid(rv),.result_ready(1'b1),
 .cnn_cycles(cc),.head_cycles(hc),.feature_cycles(fc),.parameter_write(1'b0),.parameter_bank(3'd0),
 .parameter_address(32'd0),.parameter_data(32'd0));
reg [127:0] inputs[0:575];integer cycle=0,accepted_last=0,start_at=0,feature_at_start=0;
reg [767:0] stable_filter;
 always @(posedge clk) begin
  if(dut.features_unit.accepted) stable_filter<=dut.filtered;
  if(rst_n && dut.features_unit.state==50 && dut.filtered!==stable_filter) $fatal(1,"upstream changed during lane capture");
 end
integer n,k;integer counts[0:2];
always @(posedge clk) begin
 cycle=cycle+1;
 if(sv && sr && seq==575) accepted_last=cycle;
 if(dut.start) begin start_at=cycle;feature_at_start=fc;end
end
initial begin
 $readmemh("input.hex",inputs);
 counts[0]=2;counts[1]=7;counts[2]=32;
 for(k=0;k<3;k=k+1) begin
  rst_n=0;sv=0;active=counts[k];repeat(4) @(negedge clk);rst_n=1;
  for(n=0;n<576;n=n+1) begin
   @(negedge clk);sv=1;seq=n;data=inputs[n];
   do @(posedge clk);while(!sr);
   @(negedge clk);sv=0;
  end
  wait(rv);@(posedge clk);@(negedge clk);
  if(cc!=31584 || hc<31584 || hc>45000 || (active==7 && hc!=31871) || feature_at_start!=7850 || cycle-accepted_last>50000)
   $fatal(1,"latency mismatch active=%0d cnn=%0d head=%0d feature=%0d",active,cc,hc,feature_at_start);
  $display("LATENCY_PASS active=%0d cnn=%0d head=%0d feature=%0d total=%0d start_delay=%0d",active,cc,hc,feature_at_start,cycle-accepted_last,start_at-accepted_last);
 end
 $finish;
end
initial begin #100000000;$fatal(1,"timeout");end
endmodule
