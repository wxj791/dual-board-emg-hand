`timescale 1ns/1ps
`default_nettype none
// One shared half-duplex engine. All STOP/teach completions require readback.
module b_tasks_servo #(
 parameter integer POLL_CLKS=24000,TIMEOUT_CLKS=480000,GAP_CLKS=2400,MS_CLKS=24000
)(
 input wire clk,rst_n,servo_rx,output wire servo_tx,servo_tx_enable,
 input wire req_valid,output wire req_ready,input wire [2:0] req_op,
 input wire [15:0] req_id,req_arg,
 output reg event_valid,input wire event_ready,output reg [15:0] event_id,event_status,
 output wire idle,input wire [3:0] query_pose,output wire query_valid,output wire [49:0] query_positions,
 input wire flash_init_done,flash_available,flash_load_valid,input wire [3:0] flash_load_group,
 input wire [49:0] flash_load_positions,output reg flash_save_req,output reg [3:0] flash_save_group,
 output reg [49:0] flash_save_positions,input wire flash_save_done,flash_save_ok,
 output reg [4:0] online,torque,torque_valid_mask,output reg [79:0] positions,speeds,loads,
 output wire [79:0] feedback_age_ms,output wire [4:0] feedback_valid_mask,
 output reg [8:0] pose_valid,output reg [7:0] motion_state,
 output wire [4:0] active_mask,retired_mask,output reg [31:0] fault_flags
);
 localparam IDLE=0,OFF=1,VERIFY_OFF=2,CAPTURE=3,SAVE=4,GOAL=5,ON=6,TRACK=7,POLL=8,FETCH_POSE=9;
 reg [3:0] state;reg [2:0] lane;reg [15:0] current_id;reg pending,failed,stop_wait;
 reg [15:0] waiting_stop_id;reg [3:0] pose;reg [49:0] captured;
 localparam GAP_BITS=$clog2(GAP_CLKS+1),POLL_BITS=$clog2(POLL_CLKS+1);
 (* ram_style="block" *)reg [49:0] poses[0:8];
 reg [49:0] pose_read_data,current_pose;
 wire fetch_pose=req_valid && req_ready && req_op==2;
 wire [3:0] pose_read_address=fetch_pose?req_arg[3:0]:query_pose;
 always @(posedge clk)begin
  pose_read_data<=poses[pose_read_address];
  if(flash_load_valid && flash_load_group<9)poses[flash_load_group]<=flash_load_positions;
  else if(save_inflight && flash_save_done && flash_save_ok)poses[flash_save_group]<=flash_save_positions;
 end
 reg [GAP_BITS-1:0] gap;reg [POLL_BITS-1:0] poll_age;
 reg [4:0] arrived;reg [9:0] best[0:4];reg [15:0] progress_stamp[0:4],motion_started,now_ms;
 wire [15:0] motion_ms=now_ms-motion_started,progress_ms=now_ms-progress_stamp[lane];
 reg [15:0] feedback_age[0:4];reg [14:0] ms_counter;
 genvar age_lane;
 generate for(age_lane=0;age_lane<5;age_lane=age_lane+1)begin:ages
  assign feedback_age_ms[age_lane*16+:16]=feedback_age[age_lane];
  assign feedback_valid_mask[age_lane]=online[age_lane] && feedback_age[age_lane]<200;
 end endgenerate
 reg start,inflight;reg [2:0] command;reg [9:0] target;reg [15:0] speed;
 wire engine_busy,done;wire [3:0] result;wire [7:0] error,voltage,temp,torque_read;
 wire [15:0] pos,vel,load;wire [391:0] configuration;
 reg save_inflight;
 wire response_ok=result==0;
 wire [9:0] goal=current_pose[lane*10+:10];
 wire [15:0] distance=pos>=goal?pos-goal:goal-pos;
 b_tasks_engine #(.RESPONSE_TIMEOUT_CLKS(TIMEOUT_CLKS)) engine(
  .clk(clk),.rst_n(rst_n),.SERVO_ID({5'd0,lane}+8'd1),.command_speed(speed),
  .command_valid(start),.command(command),.target_position(target),.servo_rx(servo_rx),
  .servo_tx(servo_tx),.tx_enable(servo_tx_enable),.busy(engine_busy),.response_valid(done),
  .result_code(result),.servo_error(error),.response_position(pos),.present_speed(vel),
  .present_load(load),.present_voltage(voltage),.present_temperature(temp),
  .torque_value(torque_read),.configuration(configuration));
 assign idle=state==IDLE && !pending && !stop_wait && !event_valid && !save_inflight && flash_init_done;
 assign req_ready=req_op==3 ? !stop_wait : idle;
 assign query_valid=query_pose<9?pose_valid[query_pose]:0;
 assign query_positions=query_valid?pose_read_data:0;
 assign active_mask=motion_state==1?5'b11111:0;
 assign retired_mask=5'd0;
 task finish;input [15:0] status;begin
  state<=IDLE;motion_state<=0;
  if(pending)begin event_valid<=1;event_id<=current_id;event_status<=status;pending<=0;end
 end endtask
 task launch;input [2:0] c;input [9:0] g;input [15:0] s;begin
  command<=c;target<=g;speed<=s;start<=1;inflight<=1;
 end endtask
 wire background_poll_due=1'b0;
 integer i;
 always @(posedge clk or negedge rst_n)begin
  if(!rst_n)begin
   state<=OFF;lane<=0;current_id<=0;pending<=0;failed<=0;stop_wait<=0;waiting_stop_id<=0;
   pose<=0;captured<=0;current_pose<=0;gap<=0;poll_age<=0;motion_started<=0;now_ms<=0;arrived<=0;
   start<=0;inflight<=0;command<=0;target<=0;speed<=20;
   save_inflight<=0;
   online<=0;torque<=0;torque_valid_mask<=0;positions<=0;speeds<=0;loads<=0;pose_valid<=0;motion_state<=0;fault_flags<=0;
   event_valid<=0;event_id<=0;event_status<=0;flash_save_req<=0;flash_save_group<=0;flash_save_positions<=0;
   ms_counter<=0;
   for(i=0;i<5;i=i+1)begin best[i]<=1023;progress_stamp[i]<=0;feedback_age[i]<=16'hffff;end
  end else begin
   start<=0;flash_save_req<=0;
   if(event_valid && event_ready)event_valid<=0;
   if(ms_counter==MS_CLKS-1)begin
    ms_counter<=0;now_ms<=now_ms+1'b1;for(i=0;i<5;i=i+1)if(feedback_age[i]!=16'hffff)feedback_age[i]<=feedback_age[i]+1'b1;
   end else ms_counter<=ms_counter+1'b1;
   if(gap!=0)gap<=gap-1'b1;
   if(!background_poll_due)poll_age<=poll_age+1'b1;
   if(motion_state==1)begin
    if(motion_ms>=10000)begin failed<=1;fault_flags[2]<=1;state<=OFF;lane<=0;motion_state<=0;end
   end
   if(flash_load_valid && flash_load_group<9)pose_valid[flash_load_group]<=1;
   // Flash completion is independent of the SCS inter-packet quiet interval.
   if(save_inflight && flash_save_done)begin
    save_inflight<=0;
    if(flash_save_ok)pose_valid[flash_save_group]<=1;
    else fault_flags[3]<=1;
    if(state==SAVE)finish(flash_save_ok?0:7);
   end
   if(stop_wait && !event_valid && !inflight && !engine_busy && gap==0)begin
    stop_wait<=0;pending<=1;current_id<=waiting_stop_id;failed<=0;state<=OFF;lane<=0;motion_state<=0;
   end else if( !inflight && !engine_busy && !start && gap==0)begin
    case(state)
     IDLE:if(background_poll_due)begin state<=POLL;lane<=0;poll_age<=0;end
     OFF:launch(2,0,20);
     VERIFY_OFF:launch(6,0,20);
     CAPTURE:launch(1,0,20);
     SAVE:begin end
     GOAL:launch(4,goal,20);
     FETCH_POSE:begin current_pose<=pose_read_data;state<=GOAL;end
     ON:launch(3,0,20);
     TRACK,POLL:launch(1,0,20);
    endcase
   end
   if(done && inflight)begin
    inflight<=0;gap<=GAP_CLKS;online[lane]<=response_ok;
    if(command==2 || command==3 || command==6)begin
     torque_valid_mask[lane]<=response_ok;
     if(response_ok)torque[lane]<=command==6?torque_read!=0:command==3;
    end
    if(error[0])fault_flags[8]<=1; // Voltage warning is observation only.
    if(command==1 && response_ok)begin
     positions[lane*16+:16]<=pos;speeds[lane*16+:16]<=vel;loads[lane*16+:16]<=load;
     feedback_age[lane]<=0;
    end
    case(state)
     OFF:begin
      if(!response_ok)failed<=1;
      state<=VERIFY_OFF;
     end
     VERIFY_OFF:begin
      if(!response_ok || torque_read!=0)begin failed<=1;fault_flags[0]<=1;end
      else torque[lane]<=0;
      if(lane==4)finish((failed || !response_ok || torque_read!=0)?7:0);
      else begin lane<=lane+1'b1;state<=OFF;end
     end
     CAPTURE:begin
      if(!response_ok || pos>1023)begin finish(7);fault_flags[1]<=1;end
      else begin
       captured[lane*10+:10]<=pos[9:0];
       if(lane==4)begin
        flash_save_req<=1;flash_save_group<=pose;flash_save_positions<={pos[9:0],captured[39:0]};
        save_inflight<=1;
        state<=SAVE;
       end else lane<=lane+1'b1;
      end
     end
     GOAL:begin
      if(!response_ok)begin failed<=1;state<=OFF;lane<=0;end
      else begin state<=ON;end
     end
     ON:begin
      if(!response_ok)begin failed<=1;state<=OFF;lane<=0;end
      else begin
       torque[lane]<=1;
       if(lane==4)begin lane<=0;state<=TRACK;end
       else begin lane<=lane+1'b1;state<=GOAL;end
      end
     end
     TRACK:begin
      if(!response_ok || temp>=45 || pos>1023 || load[9:0]>=300 || progress_ms>=1000)begin
       failed<=1;fault_flags[2]<=1;state<=OFF;lane<=0;
      end else begin
       if(distance<=4)begin arrived[lane]<=1;progress_stamp[lane]<=now_ms;end
       else begin
        arrived[lane]<=0;
        if(distance+2<=best[lane])begin best[lane]<=distance[9:0];progress_stamp[lane]<=now_ms;end
       end
       if(&arrived && distance<=4)finish(0);
       else lane<=lane==4?0:lane+1'b1;
      end
     end
     POLL:if(lane==4)state<=IDLE;else lane<=lane+1'b1;
    endcase
   end
   if(req_valid && req_ready)begin
    if(req_op==3)begin
     // Cancel the old operation first; STOP has its own final event.
     if(pending)begin event_valid<=1;event_id<=current_id;event_status<=7;pending<=0;end
     stop_wait<=1;waiting_stop_id<=req_id;
     motion_state<=0;
    end else begin
     current_id<=req_id;pending<=1;failed<=0;lane<=0;pose<=req_arg[3:0];
     if(req_op==1 && req_arg<9 && torque==0 && flash_available)begin state<=CAPTURE;motion_state<=3;end
     else if(req_op==2 && req_arg<9 && pose_valid[req_arg[3:0]])begin
      state<=FETCH_POSE;motion_state<=1;arrived<=0;motion_started<=now_ms;
      for(i=0;i<5;i=i+1)begin best[i]<=1023;progress_stamp[i]<=now_ms;end
     end
     else begin event_valid<=1;event_id<=req_id;event_status<=5;pending<=0;end
    end
   end
  end
 end
endmodule
`default_nettype wire
