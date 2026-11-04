if {[catch {
set release_dir [file dirname [info script]]
set rebuild_dir [file join $release_dir rebuild_[clock format [clock seconds] -format %Y%m%d_%H%M%S]]
if {[file exists $rebuild_dir]} {error "Rebuild directory already exists"}
file mkdir $rebuild_dir
foreach source [glob -directory $release_dir *.hex] {file copy $source $rebuild_dir}
foreach name {all.v v2.adc v2.sdc v2_place.sdc} {file copy [file join $release_dir $name] $rebuild_dir}
cd $rebuild_dir
import_device eagle_s20.db -package EG4S20BG256
read_verilog -file all.v -top v2_board_top
read_adc v2.adc
read_sdc v2.sdc


set_param gate opt_area high
set_param gate pack_effort high
commit_param -step flow
commit_param -step gate
optimize_rtl
optimize_gate -maparea gate.area
write_verilog mapped.v
export_db mapped.db
set area_file [open gate.area r]
set area_text [read $area_file]
close $area_file
foreach {key limit} {lut 19600 reg 19600 bram 64 bram32k 16 dsp 29} {
    if {[regexp -line [format {^#%s\s+(\d+)} $key] $area_text unused count] && $count>$limit} {error "CAPACITY_RESOURCE_FAILURE $key=$count limit=$limit"}
}

set_param place pr_strategy 5
set_param place seed 17
set_param flow enable_seed on
commit_param -step flow
commit_param -step place
clear_sdc
read_sdc v2_place.sdc
start_timer
set_param route effort routability
commit_param -step route
legalize_phy_inst
place
route
clear_sdc
read_sdc v2.sdc
start_timer
update_timing -mode final
fix_hold
report_area -io_info -file phy.area
report_timing_summary -file final_timing.rpt
check_timing -verbose -file timing_check.rpt
report_route_status -drc
export_db routed.db
write_verilog routed.v
bitgen -bit a_gesture_tasks.bit
puts "A_GESTURE_TASKS_BUILD_PASS"

} err]} {puts stderr $err; exit 1}
exit
