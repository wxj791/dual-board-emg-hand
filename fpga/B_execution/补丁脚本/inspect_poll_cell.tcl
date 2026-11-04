if {[catch {
set root [file normalize [file join [file dirname [info script]] ../..]]
cd [file join $root artifacts b_gesture_tasks td_eco_inspect]
import_db [file normalize [file join $root .. r53_wave_fix_20261102 artifacts b_gesture_tasks td_r53_nc1 routed.db]]
set cells [get_cells -regexp {control/json_ages_reg.32._syn_4}]
puts "POLL_PACKED_CELL_MATCHES $cells"
if {[llength $cells]!=1} {error "Expected one packed cell"}
report_property $cells
report_cells $cells -connection -pin_loc
puts POLL_PACKED_CELL_INSPECT_DONE
} message]} {puts stderr $message; exit 1}
exit
