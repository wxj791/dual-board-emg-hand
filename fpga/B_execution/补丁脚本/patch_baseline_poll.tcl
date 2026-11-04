if {[catch {
set root [file normalize [file join [file dirname [info script]] ../..]]
set work [file join $root artifacts b_gesture_tasks td_eco_poll]
file mkdir $work
cd $work
import_db [file normalize [file join $root .. r53_wave_fix_20261102 artifacts b_gesture_tasks td_r53_nc1 routed.db]]
set cells [get_cells -regexp {control/json_ages_reg.32._syn_4}]
if {[llength $cells]!=1} {error "Expected one packed cell"}
puts "BEFORE [get_property ATTRIBUTES $cells]"
set_inst_attr -model b_tasks_top -inst {control/json_ages_reg[32]_syn_4} -attr_key LUT0 -attr_value {=0}
puts "AFTER [get_property ATTRIBUTES $cells]"
write_verilog patched_physical.v
export_db patched.db
puts BASELINE_POLL_PATCH_EXPORTED
} message]} {puts stderr $message; exit 1}
exit
