`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;reg rst_n=0,wireless_rx=1,pc_rx=1,ready=1;
wire pc_tx,lclk,valid;wire [7:0] data;wire [3:0] led;
v2_board_top #(.CAPACITY(32)) dut(clk,rst_n,pc_rx,pc_tx,wireless_rx,1'b1,data,lclk,valid,ready,led);
reg [7:0] input_bytes[0:43775],queries[0:43];
reg [831:0] expected[0:1215];reg [63:0] decisions[0:20];
reg [7:0] model_commands[0:10550];reg [31:0] model_bounds[0:25];integer model_i,model_j,model_start=0;
integer n,j,k,p,q,sample_count=0,result_count=0,link_file,pc_file,cycle=0,max_latency=0;
integer accepted_at[0:1215];
wire [7:0] pc_byte;wire pc_bv;
ingress_uart_rx monitor(clk,dut.reset_n,pc_tx,pc_byte,pc_bv,/*framing*/,/*busy*/);
always @(posedge lclk) if(valid && ready) $fdisplay(link_file,"%02x",data);
always @(posedge clk) begin
 cycle=cycle+1;
 if(pc_bv) $fdisplay(pc_file,"%02x",pc_byte);
 if(dut.input_valid && dut.input_ready) accepted_at[dut.sample_sequence]=cycle;
 if(dut.tv && dut.core_reset) begin
  if(dut.wave_payload!==expected[sample_count]) $fatal(1,"four-group sample mismatch n=%0d seq=%0d raw=%h filt=%h fast=%h slow=%h",sample_count,dut.tseq,dut.traw,dut.tfilt,dut.tfast,dut.tslow);
  sample_count=sample_count+1;
 end
 if(dut.rv && dut.core_reset) begin
  if(dut.raw_class!==decisions[result_count][15:0] || dut.stable_class!==decisions[result_count][31:16] ||
     dut.margin!==decisions[result_count][40:32] || dut.action!==decisions[result_count][63:48] || dut.rseq!==575+32*result_count)
   $fatal(1,"classification mismatch result=%0d seq=%0d",result_count,dut.rseq);
  if(dut.cnn_cycles!=31584) $fatal(1,"CNN cycle regression");
  if(cycle-accepted_at[dut.rseq]>max_latency) max_latency=cycle-accepted_at[dut.rseq];
  $display("BOARD_RESULT seq=%0d cnn=%0d head=%0d feature=%0d latency=%0d",dut.rseq,dut.cnn_cycles,dut.head_cycles,dut.feature_cycles,cycle-accepted_at[dut.rseq]);
  result_count=result_count+1;
 end
end
initial begin
 link_file=$fopen("link_capture.hex","w");pc_file=$fopen("pc_capture.hex","w");
 $readmemh("wireless_input.hex",input_bytes);$readmemh("expected_wave.hex",expected);$readmemh("expected_decision.hex",decisions);$readmemh("pc_queries.hex",queries);
 repeat(5) @(negedge clk);rst_n=1;repeat(30) @(negedge clk);
 $readmemh("model_commands.hex",model_commands);$readmemh("model_bounds.hex",model_bounds);
 for(model_i=0;model_i<26;model_i=model_i+1) begin
  for(model_j=model_start;model_j<model_bounds[model_i];model_j=model_j+1) begin
   pc_rx=0;repeat(26)@(negedge clk);
   for(integer model_bit=0;model_bit<8;model_bit=model_bit+1) begin pc_rx=model_commands[model_j][model_bit];repeat(26)@(negedge clk);end
   pc_rx=1;repeat(26)@(negedge clk);
  end
  model_start=model_bounds[model_i];repeat(1000)@(negedge clk);
  while(dut.pc.state!=0 || dut.pc.tx_state!=0)@(negedge clk);
 end
 repeat(10000)@(negedge clk);
 if(!dut.model_valid || dut.generation!=1 || dut.active_classes!=2 || dut.pc.model_crc32!=32'h624382bb)$fatal(1,"fresh model deployment failed");
 $display("FRESH_MODEL_UART_DEPLOYED classes=2 crc=624382bb");
 fork
 begin
  for(n=0;n<1216;n=n+1) begin
   if(n==60) ready=0;if(n==68) ready=1;
   for(j=0;j<36;j=j+1) begin
    wireless_rx=0;repeat(26) @(negedge clk);
    for(k=0;k<8;k=k+1) begin wireless_rx=input_bytes[n*36+j][k];repeat(26) @(negedge clk);end
    wireless_rx=1;repeat(26) @(negedge clk);
   end
   repeat(24000-36*260) @(negedge clk);
  end
 end
 begin
  repeat(100000) @(negedge clk);
  for(p=0;p<44;p=p+1) begin
   if(p==22) repeat(200000) @(negedge clk);
   pc_rx=0;repeat(26) @(negedge clk);
   for(q=0;q<8;q=q+1) begin pc_rx=queries[p][q];repeat(26) @(negedge clk);end
   pc_rx=1;repeat(26) @(negedge clk);
  end
 end
 join
 repeat(200000) @(negedge clk);
 if(sample_count!=1216 || result_count!=21 || dut.overflows!=0 || dut.result_drops!=0 || dut.wave_drops==0)
  $fatal(1,"coverage samples=%0d results=%0d wave_drop=%0d result_drop=%0d",sample_count,result_count,dut.wave_drops,dut.result_drops);
 $display("BOARD_PASS samples=%0d results=%0d wave_drops=%0d max_latency=%0d",sample_count,result_count,dut.wave_drops,max_latency);
 $fclose(link_file);$fclose(pc_file);$finish;
end
initial begin #2000000000;$fatal(1,"timeout");end
endmodule
