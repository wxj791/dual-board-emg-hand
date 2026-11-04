`timescale 1ns/1ps
module tb;
reg clk=0;always #5 clk=~clk;
reg rst_n=0,fw=0,ew=0,start=0,done=0,pw=0;
reg [8:0] fa=0;reg signed [15:0] fd=0;reg [4:0] ea=0;reg signed [7:0] ed=0;
reg [2:0] pb=0;reg [12:0] pa=0;reg [31:0] pd=0;
wire ready,rv;wire [15:0] raw,stable,cnn,action;wire [8:0] margin;wire [335:0] score,correction;wire [31:0] cycles;
fusion_head #(.EXPORT_SCORES(1)) dut(clk,rst_n,16'd7,fw,fa,fd,ew,ea,ed,start,ready,done,rv,1'b1,
 raw,stable,cnn,action,margin,score,correction,cycles,pw,pb,pa,pd);
reg [15:0] features[0:8207];reg [7:0] embedding[0:863];
reg [47:0] scores[0:377],decision[0:53];
integer phase,w,i,results=0;
initial begin
 $readmemh("features.hex",features);$readmemh("embedding.hex",embedding);
 $readmemh("scores.hex",scores);$readmemh("decision.hex",decision);
 for(phase=0;phase<2;phase=phase+1) begin
  rst_n=0;repeat(3) @(negedge clk);rst_n=1;
  if(phase==0) begin
   dut.fusion_threshold=0;
   pw=1;pb=4;pa=153;pd=2097152;@(negedge clk);pw=0;
   if(dut.fusion_threshold!=2097152) $fatal(1,"confidence threshold reload");
  end
  if(phase==1) begin
   // Erase the learned FC weights and bias via the real parameter port.
   for(i=0;i<224;i=i+1) begin pw=1;pb=5;pa=i;pd=0;@(negedge clk);end
   for(i=0;i<7;i=i+1) begin pw=1;pb=6;pa=i*2;pd=0;@(negedge clk);end
   pw=0;
  end
  for(w=0;w<27;w=w+1) begin
   while(!ready) @(negedge clk);
   for(i=0;i<304;i=i+1) begin fw=1;fa=i;fd=features[w*304+i];@(negedge clk);end
   fw=0;
   for(i=0;i<32;i=i+1) begin ew=1;ea=i;ed=embedding[w*32+i];@(negedge clk);end
   ew=0;start=1;@(negedge clk);start=0;repeat(5) @(negedge clk);
   done=1;@(negedge clk);done=0;
   while(!rv) @(negedge clk);
   for(i=0;i<7;i=i+1) if(score[i*48 +:48]!==scores[(phase*27+w)*7+i])
    $fatal(1,"residual score phase=%0d window=%0d class=%0d",phase,w,i);
   if(raw!==decision[phase*27+w][3:0] || stable!==decision[phase*27+w][19:16] || margin!==decision[phase*27+w][40:32])
    $fatal(1,"residual decision phase=%0d window=%0d",phase,w);
   results=results+1;@(negedge clk);
  end
 end
 $display("RESIDUAL_HEAD_ABLATION_PASS windows=%0d phases=2 results=%0d",27,results);$finish;
end
initial begin #100000000;$fatal(1,"timeout");end
endmodule
