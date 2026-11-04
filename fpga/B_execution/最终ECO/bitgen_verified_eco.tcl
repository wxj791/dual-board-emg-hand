if {[catch {
cd {E:/FPGA/Projects/emg_hand_fpga/artifacts/pc_gesture_tasks/pc_candidate_20261101_180659_073399/r53_servo_schedule_20261102/artifacts/b_gesture_tasks/td_eco_poll}
import_db patched.db
set cells [get_cells -regexp {control/json_ages_reg.32._syn_4}]
if {[llength $cells]!=1} {error "ECO cell missing"}
set attrs [get_property ATTRIBUTES $cells]
if {![string match {*LUT0 = =0;*} $attrs]} {error "ECO LUT mismatch"}
bitgen -bit B_R53_ECO_NO_IDLE_POLL_20261102.bit
puts POLL_ECO_BITGEN_PASS
} message]} {puts stderr $message; exit 1}
exit
