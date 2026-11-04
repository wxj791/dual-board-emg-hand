if(data_length==0 || arguments[7:0]==0)begin
json_slot<=3'd1;response_length<=10'd395;
json_words0<=positions;json_words1<=speeds;json_loads<=loads;json_ages<=feedback_age_ms;
json_online<=online;json_torque<=torque;json_valid<=feedback_valid_mask;json_faults<=fault_flags;json_torque_valid<=torque_valid_mask;
end else begin
case(arguments[2:0])
3'd1:begin
json_slot<=3'd2;response_length<=10'd259;
end
3'd2:begin
json_slot<=3'd3;response_length<=10'd247;
end
3'd3:begin
json_slot<=3'd4;response_length<=10'd255;
end
3'd4:begin
json_slot<=3'd5;response_length<=10'd255;
end
default:reply(3);
endcase
for(integer q=0;q<8;q=q+1)begin json_words0[q*16+:16]<=range_value(range_pages[{json_query_page,q[2:0]}]);json_words1[q*16+:16]<=gain_pages[{json_query_page,q[2:0]}];end
json_custom<=custom_pages[{json_query_page,3'd0}+:8];
end
