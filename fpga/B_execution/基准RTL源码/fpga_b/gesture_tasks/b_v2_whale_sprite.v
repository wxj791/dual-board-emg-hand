`timescale 1ns/1ps
`default_nettype none
// Three-clock packed ROM pipeline. Explicit local registers isolate the slow
// 32K RAM outputs from bank/nibble selection at the 148.5MHz pixel clock.
module b_v2_whale_sprite(input wire clk,input wire [4:0] frame,
 input wire [5:0] x,y,output wire [23:0] rgb);
 wire [11:0] address={frame[0],y,x[5:1]};
 (* ram_style="bram_32k" *) reg [7:0] packed0[0:4095];
 reg [7:0] q0;(* preserve="true" *) reg [7:0] delay0;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed0.hex",packed0);
 always @(posedge clk)begin q0<=packed0[address];delay0<=q0;end
 (* ram_style="bram_32k" *) reg [7:0] packed1[0:4095];
 reg [7:0] q1;(* preserve="true" *) reg [7:0] delay1;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed1.hex",packed1);
 always @(posedge clk)begin q1<=packed1[address];delay1<=q1;end
 (* ram_style="bram_32k" *) reg [7:0] packed2[0:4095];
 reg [7:0] q2;(* preserve="true" *) reg [7:0] delay2;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed2.hex",packed2);
 always @(posedge clk)begin q2<=packed2[address];delay2<=q2;end
 (* ram_style="bram_32k" *) reg [7:0] packed3[0:4095];
 reg [7:0] q3;(* preserve="true" *) reg [7:0] delay3;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed3.hex",packed3);
 always @(posedge clk)begin q3<=packed3[address];delay3<=q3;end
 (* ram_style="bram_32k" *) reg [7:0] packed4[0:4095];
 reg [7:0] q4;(* preserve="true" *) reg [7:0] delay4;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed4.hex",packed4);
 always @(posedge clk)begin q4<=packed4[address];delay4<=q4;end
 (* ram_style="bram_32k" *) reg [7:0] packed5[0:4095];
 reg [7:0] q5;(* preserve="true" *) reg [7:0] delay5;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed5.hex",packed5);
 always @(posedge clk)begin q5<=packed5[address];delay5<=q5;end
 (* ram_style="bram_32k" *) reg [7:0] packed6[0:4095];
 reg [7:0] q6;(* preserve="true" *) reg [7:0] delay6;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed6.hex",packed6);
 always @(posedge clk)begin q6<=packed6[address];delay6<=q6;end
 (* ram_style="bram_32k" *) reg [7:0] packed7[0:4095];
 reg [7:0] q7;(* preserve="true" *) reg [7:0] delay7;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed7.hex",packed7);
 always @(posedge clk)begin q7<=packed7[address];delay7<=q7;end
 (* ram_style="bram_32k" *) reg [7:0] packed8[0:4095];
 reg [7:0] q8;(* preserve="true" *) reg [7:0] delay8;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed8.hex",packed8);
 always @(posedge clk)begin q8<=packed8[address];delay8<=q8;end
 (* ram_style="bram_32k" *) reg [7:0] packed9[0:4095];
 reg [7:0] q9;(* preserve="true" *) reg [7:0] delay9;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed9.hex",packed9);
 always @(posedge clk)begin q9<=packed9[address];delay9<=q9;end
 (* ram_style="bram_32k" *) reg [7:0] packed10[0:4095];
 reg [7:0] q10;(* preserve="true" *) reg [7:0] delay10;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed10.hex",packed10);
 always @(posedge clk)begin q10<=packed10[address];delay10<=q10;end
 (* ram_style="bram_32k" *) reg [7:0] packed11[0:4095];
 reg [7:0] q11;(* preserve="true" *) reg [7:0] delay11;
 initial $readmemh("fpga_b/ab_scope_v2/assets/whale/packed11.hex",packed11);
 always @(posedge clk)begin q11<=packed11[address];delay11<=q11;end
 reg [3:0] bank1;(* preserve="true" *) reg [23:0] bank2;reg half1,half2;reg [3:0] palette_index;
 reg [7:0] selected;
 always @*selected=({8{bank2[0]}} & delay0)|({8{bank2[1]}} & delay1)|({8{bank2[2]}} & delay2)|({8{bank2[3]}} & delay3)|({8{bank2[4]}} & delay4)|({8{bank2[5]}} & delay5)|({8{bank2[6]}} & delay6)|({8{bank2[7]}} & delay7)|({8{bank2[8]}} & delay8)|({8{bank2[9]}} & delay9)|({8{bank2[10]}} & delay10)|({8{bank2[11]}} & delay11);
 always @(posedge clk)begin
  bank1<=frame[4:1];bank2<=24'b1<<({bank1,1'b0}+half1);half1<=x[0];half2<=half1;
  palette_index<=({4{bank2[0]}} & delay0[3:0])|({4{bank2[1]}} & delay0[7:4])|({4{bank2[2]}} & delay1[3:0])|({4{bank2[3]}} & delay1[7:4])|({4{bank2[4]}} & delay2[3:0])|({4{bank2[5]}} & delay2[7:4])|({4{bank2[6]}} & delay3[3:0])|({4{bank2[7]}} & delay3[7:4])|({4{bank2[8]}} & delay4[3:0])|({4{bank2[9]}} & delay4[7:4])|({4{bank2[10]}} & delay5[3:0])|({4{bank2[11]}} & delay5[7:4])|({4{bank2[12]}} & delay6[3:0])|({4{bank2[13]}} & delay6[7:4])|({4{bank2[14]}} & delay7[3:0])|({4{bank2[15]}} & delay7[7:4])|({4{bank2[16]}} & delay8[3:0])|({4{bank2[17]}} & delay8[7:4])|({4{bank2[18]}} & delay9[3:0])|({4{bank2[19]}} & delay9[7:4])|({4{bank2[20]}} & delay10[3:0])|({4{bank2[21]}} & delay10[7:4])|({4{bank2[22]}} & delay11[3:0])|({4{bank2[23]}} & delay11[7:4]);
 end
 localparam [15:0] P0=16'hc3d6; assign rgb[0]=P0[palette_index];
 localparam [15:0] P1=16'hc41c; assign rgb[1]=P1[palette_index];
 localparam [15:0] P2=16'hc2b2; assign rgb[2]=P2[palette_index];
 localparam [15:0] P3=16'h3c2f; assign rgb[3]=P3[palette_index];
 localparam [15:0] P4=16'hfe13; assign rgb[4]=P4[palette_index];
 localparam [15:0] P5=16'h023e; assign rgb[5]=P5[palette_index];
 localparam [15:0] P6=16'h01ee; assign rgb[6]=P6[palette_index];
 localparam [15:0] P7=16'h00fe; assign rgb[7]=P7[palette_index];
 localparam [15:0] P8=16'h0298; assign rgb[8]=P8[palette_index];
 localparam [15:0] P9=16'h81d2; assign rgb[9]=P9[palette_index];
 localparam [15:0] P10=16'h7e4f; assign rgb[10]=P10[palette_index];
 localparam [15:0] P11=16'hfcbb; assign rgb[11]=P11[palette_index];
 localparam [15:0] P12=16'h026e; assign rgb[12]=P12[palette_index];
 localparam [15:0] P13=16'h0126; assign rgb[13]=P13[palette_index];
 localparam [15:0] P14=16'h00ee; assign rgb[14]=P14[palette_index];
 localparam [15:0] P15=16'h001e; assign rgb[15]=P15[palette_index];
 localparam [15:0] P16=16'hab20; assign rgb[16]=P16[palette_index];
 localparam [15:0] P17=16'ha142; assign rgb[17]=P17[palette_index];
 localparam [15:0] P18=16'ha1f6; assign rgb[18]=P18[palette_index];
 localparam [15:0] P19=16'h5e5f; assign rgb[19]=P19[palette_index];
 localparam [15:0] P20=16'h0196; assign rgb[20]=P20[palette_index];
 localparam [15:0] P21=16'h0076; assign rgb[21]=P21[palette_index];
 localparam [15:0] P22=16'h001e; assign rgb[22]=P22[palette_index];
 localparam [15:0] P23=16'h000e; assign rgb[23]=P23[palette_index];
endmodule
`default_nettype wire
