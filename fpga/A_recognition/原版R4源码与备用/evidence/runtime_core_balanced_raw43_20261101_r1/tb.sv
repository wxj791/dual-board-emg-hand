`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;
reg rst=0,sv=0,pw=0;reg [127:0] data=0;reg [31:0] seq=0;
reg [2:0] pb=0;reg [31:0] pa=0,pd=0;
wire sr,rv,tv,pr;wire [15:0] raw,stable,action;wire [8:0] margin;
wire [31:0] rseq,tseq,cc,hc;wire [127:0] traw;wire [191:0] filt,fast,slow;
fusion_cnn_stream #(.CLASSES(32)) dut(.clk(clk),.rst_n(rst),.active_classes(16'd2),
 .sample_seq(seq),.sample_valid(sv),.sample_ready(sr),.sample_mv(data),
 .result_valid(rv),.result_ready(1'b1),.raw_class(raw),.stable_class(stable),.action_id(action),.margin_q8(margin),
 .cnn_cycles(cc),.head_cycles(hc),.parameter_write(pw),.parameter_bank(pb),.parameter_address(pa),.parameter_data(pd),
 .parameter_ready(pr),.telemetry_valid(tv),.telemetry_sample_seq(tseq),.result_sample_seq(rseq),
 .telemetry_raw(traw),.telemetry_filtered(filt),.telemetry_fast(fast),.telemetry_slow(slow));
reg [831:0] waves[0:1215];reg [63:0] decisions[0:20];
reg [67:0] writes[0:2425];integer i,n,tc=0,rc=0,cycles=0,max_latency=0;
integer accepted_at[0:1215];
always @(posedge clk) begin
 cycles=cycles+1;
 if(rst && sv && sr) accepted_at[seq]=cycles;
 if(rst && tv) begin
  if(tseq!==tc || {slow,fast,filt,traw}!==waves[tc][831:128])$fatal(1,"telemetry mismatch %0d",tc);
  tc=tc+1;
 end
 if(rst && rv) begin
  if(rseq!==575+32*rc || raw!==decisions[rc][15:0] || stable!==decisions[rc][31:16] ||
     margin!==decisions[rc][40:32] || action!==decisions[rc][63:48])$fatal(1,"result mismatch %0d",rc);
  if(cc!=31584 || cycles-accepted_at[rseq]>50000)$fatal(1,"cycle regression");
  if(cycles-accepted_at[rseq]>max_latency)max_latency=cycles-accepted_at[rseq];
  $display("CORE_RESULT seq=%0d raw=%0d stable=%0d margin=%0d latency=%0d",rseq,raw,stable,margin,cycles-accepted_at[rseq]);
  rc=rc+1;
 end
end
initial begin
 $readmemh("expected_wave.hex",waves);$readmemh("expected_decision.hex",decisions);$readmemh("parameter_writes.hex",writes);
 repeat(4)@(negedge clk);rst=1;repeat(4)@(negedge clk);
 if(!pr)$fatal(1,"core not ready for parameters");
 for(i=0;i<2426;i=i+1) begin
  pw=1;pb=writes[i][66:64];pa=writes[i][63:32];pd=writes[i][31:0];@(negedge clk);
 end
 pw=0;rst=0;repeat(4)@(negedge clk);rst=1;repeat(4)@(negedge clk);
 for(n=0;n<1216;n=n+1)begin
  @(negedge clk);sv=1;seq=n;data=waves[n][255:128];
  do @(posedge clk);while(!sr);
  @(negedge clk);sv=0;
 end
 wait(rc==21 && tc==1216);repeat(5)@(negedge clk);
 $display("RUNTIME_CORE_PASS samples=%0d results=%0d writes=2426 max_latency=%0d",tc,rc,max_latency);$finish;
end
initial begin #200000000;$fatal(1,"timeout");end
endmodule
