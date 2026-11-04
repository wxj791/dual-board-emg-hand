if {[catch {
# Private r50 EDA. No cable/JTAG/programming commands, no old outputs.
set root [file normalize [file join [file dirname [info script]] ..]]
if {[info exists ::env(B_TASKS_ROOT)]} {set root $::env(B_TASKS_ROOT)}
set build_dir $::env(B_TASKS_BUILD_DIR)
file mkdir $build_dir
cd $root
if {[info exists ::env(B_TASKS_RESUME_MAPPED)]} {
 cd $build_dir
 import_db $::env(B_TASKS_RESUME_MAPPED)
} else {
set f [open fpga_b/gesture_tasks/sources.txt r]
set sources [split [string trim [read $f]] "\n"]
close $f
set combined [open [file join $build_dir all.v] w]
foreach source $sources {
 set input [open [string trim $source] r]
 set body [read $input]
 close $input
 foreach inc {caps status} {
  set input [open fpga_b/gesture_tasks/$inc.vh r]
  set body [string map [list "`include \"fpga_b/gesture_tasks/$inc.vh\"" [read $input]] $body]
  close $input
 }
 set body [string map [list "fpga_b/pc_scope_1080_fine_exp/assets/" "$root/fpga_b/pc_scope_1080_fine_exp/assets/" "fpga_b/pc_scope_servo/assets/" "$root/fpga_b/pc_scope_servo/assets/" "fpga_b/ab_scope_v2/assets/" "$root/fpga_b/ab_scope_v2/assets/" "fpga_b/gesture_tasks/assets/" "$root/fpga_b/gesture_tasks/assets/"] $body]
 puts $combined $body
}
close $combined
cd $build_dir
import_device eagle_s20.db -package EG4S20BG256
read_verilog -file all.v -top b_tasks_top
read_adc [file join $root fpga_b gesture_tasks b_tasks.adc]
read_sdc [file join $root fpga_b gesture_tasks b_tasks.sdc]
read_sdc [file join $root fpga_b gesture_tasks b_tasks_mean_multicycle.sdc]
set_param rtl min_ripple_len 8
optimize_rtl
report_area -file rtl.area
set_param gate map_mode area_prior
set_param gate opt_area high
set_param gate pack_effort high
commit_param -step gate
optimize_gate -maparea gate.area
write_verilog mapped.v
export_db mapped.db
}
set area_file [open gate.area r]
set area_text [read $area_file]
close $area_file
foreach {key limit} {lut 19600 reg 19600 bram 64 bram32k 16 dsp 29} {
 if {[regexp -line [format {^#%s\s+(\d+)} $key] $area_text unused count] && $count>$limit} {error "CAPACITY_RESOURCE_FAILURE $key=$count"}
}
legalize_phy_inst
write_verilog gate.v
set_param place pr_strategy $::env(B_TASKS_PR_STRATEGY)
set_param place pack_mode $::env(B_TASKS_PACK_MODE)
set_param place seed $::env(B_TASKS_SEED)
if {$::env(B_TASKS_CONGESTION) eq "medium"} {
 # Official PH1 CongestionMedium preset; timing constraints stay unchanged.
 foreach {key value} {opt_timing high ofv 70 opt_max_density 50 target_density 0.7 target_density_pp 0.75 aggressive_opt 20 enhanced_beeline_opt 2 enhanced_gp on non_crit_opt on post_place_opt high} {
  set_param place $key $value
 }
}
commit_param -step place
if {[catch {place} place_error]} {
 puts "B_TASKS_PLACE_FAILED <$place_error>"
 catch {report_area -file failed_place.area}
 catch {export_db failed_place.db}
 exit 1
}
export_db placed.db
report_timing_summary -ep_num 30 -file placed_timing.rpt
report_area -io_info -file placed.area
set_param route effort $::env(B_TASKS_ROUTE_EFFORT)
set_param route fix_hold off
set_param route post_route_opt high
commit_param -step route
if {[catch {route} route_error]} {
 catch {reset_path_margin -all}
 catch {report_timing_summary -ep_num 30 -file failed_route_timing.rpt}
 catch {report_area -io_info -file failed_route.area}
 catch {export_db failed_route.db}
 error "B_TASKS_ROUTE_FAILED <$route_error>"
}
export_db pre_hold.db
import_db pre_hold.db
fix_hold
reset_path_margin -all
report_area -io_info -file phy.area
report_timing_summary -ep_num 30 -file final_timing.rpt
check_timing -verbose -file timing_check.rpt
report_timing_exception -file timing_exceptions.rpt
report_clock_summary -file clocks.rpt
export_db routed.db
report_route_status -drc
set f [open final_timing.rpt r]
set timing [read $f]
close $f
foreach metric {SWNS STNS HWNS HTNS} {
 if {![regexp "$metric: +(-?\[0-9.\]+)ns" $timing unused value]} {error "Missing $metric"}
 if {$value < 0} {error "R50_REJECT $metric=$value"}
}
bitgen -bit fpga_b_gesture_tasks_r50.bit
puts B_GESTURE_TASKS_R50_TD_PASS

} err]} {puts stderr $err; exit 1}
exit
