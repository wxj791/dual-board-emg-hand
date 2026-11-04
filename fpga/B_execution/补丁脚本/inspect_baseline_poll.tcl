if {[catch {
set root [file normalize [file join [file dirname [info script]] ../..]]
set work [file join $root artifacts b_gesture_tasks td_eco_inspect]
file mkdir $work
cd $work
import_db [file normalize [file join $root .. r53_wave_fix_20261102 artifacts b_gesture_tasks td_r53_nc1 routed.db]]
write_verilog baseline_physical.v
set cells [get_cells -regexp {.*servo_controller.*lane_reg.*syn_12.*}]
puts "POLL_CELL_MATCHES $cells"
report_property $cells
report_cells $cells -connection -pin_loc
puts BASELINE_POLL_INSPECT_DONE
} message]} {puts stderr $message; exit 1}
exit
