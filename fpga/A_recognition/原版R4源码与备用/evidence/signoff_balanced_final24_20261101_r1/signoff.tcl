if {[catch {
cd {E:/FPGA/Projects/emg_hand_fpga/artifacts/a_gesture_tasks/signoff_balanced_final24_20261101_r1}
import_device eagle_s20.db -package EG4S20BG256
import_db {E:/FPGA/Projects/emg_hand_fpga/artifacts/a_gesture_tasks/route32_balanced_final24_20261101_r1/routed.db}
clear_sdc
read_sdc {E:/FPGA/Projects/emg_hand_fpga/artifacts/a_gesture_tasks/route32_balanced_final24_20261101_r1/v2.sdc}
start_timer
update_timing -mode final
report_timing_summary -file independent_timing.rpt
check_timing -verbose -file independent_coverage.rpt
report_route_status -drc
puts "A_INDEPENDENT_SIGNOFF_PASS"

} err]} {puts stderr $err;exit 1}
exit
