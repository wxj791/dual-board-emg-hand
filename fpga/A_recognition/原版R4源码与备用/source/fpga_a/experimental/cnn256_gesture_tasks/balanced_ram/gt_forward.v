`default_nettype none
// One ordinary packet + a separate STOP slot. No command is retried.
// The slot remains occupied until the complete payload has been consumed.
module gt_forward(
 input wire clk,rst_n,flush,
 input wire start,input wire is_stop,input wire [6:0] length,
 output wire ready,input wire valid,output wire take,input wire [7:0] data,
 output wire frame_valid,input wire frame_ready,output wire frame_stop,
 output wire [10:0] frame_length,output wire payload_valid,
 input wire payload_ready,output wire [7:0] payload_data,
 output reg [15:0] cancelled,output wire [3:0] queue_state
);
 (* ram_style="bram" *) reg [7:0] memory[0:63];
 reg [7:0] q;reg [5:0] wr,rd;
 reg writing,write_stop,pending,stop_pending,active,active_stop;
 reg [6:0] size,left,write_left;reg [63:0] stop_header;
 assign ready=!writing && (is_stop ? !stop_pending && !(active && active_stop) : !pending && !(active && !active_stop));
 assign take=writing;
 assign frame_stop=stop_pending;
 assign frame_valid=!active && (pending || stop_pending) && !flush && !writing;
 assign frame_length=stop_pending ? 11'd8 : {4'b0,size};
 assign payload_valid=active;
 assign payload_data=active_stop ? stop_header[7:0] : q;
 assign queue_state={active,writing,stop_pending,pending};
 wire [5:0] prefetch=(active && !active_stop && payload_ready && left>1) ? rd+1'b1 : rd;
 always @(posedge clk) begin
  q<=memory[prefetch];
  if(writing && valid && !write_stop) memory[wr]<=data;
 end
 always @(posedge clk or negedge rst_n) begin
  if(!rst_n) begin
   wr<=0;rd<=0;writing<=0;write_stop<=0;pending<=0;stop_pending<=0;
   active<=0;active_stop<=0;size<=0;left<=0;write_left<=0;stop_header<=0;cancelled<=0;
  end else begin
   if(flush) begin
    pending<=0;stop_pending<=0;
    if(pending || stop_pending || writing) cancelled<=cancelled+1'b1;
    // An already started frame always finishes. PC enqueue cannot overlap
    // local MODEL_BEGIN; a stream discontinuity during a copy cancels it.
    writing<=0;
   end else begin
    if(start && ready) begin writing<=1;write_stop<=is_stop;wr<=0;write_left<=length;if(!is_stop) size<=length;end
    if(writing && valid) begin
     if(write_stop) stop_header[wr*8 +:8]<=data;
     wr<=wr+1'b1;write_left<=write_left-1'b1;
     if(write_left==1) begin writing<=0;if(write_stop) stop_pending<=1;else pending<=1;end
    end
    if(frame_valid && frame_ready) begin
     active<=1;active_stop<=stop_pending;rd<=0;
     left<=stop_pending ? 7'd8 : size;
     if(stop_pending) stop_pending<=0;else pending<=0;
    end
   end
   if(active && payload_ready) begin
    if(active_stop) stop_header<=stop_header>>8;else rd<=rd+1'b1;
    left<=left-1'b1;if(left==1) active<=0;
   end
  end
 end
endmodule
`default_nettype wire
