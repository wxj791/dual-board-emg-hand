if {[catch {
set root [file normalize [file join [file dirname [info script]] ../..]]
cd [file join $root artifacts b_gesture_tasks td_eco_poll]
import_db patched.db
write_verilog reloaded_physical.v
reset_path_margin -all
report_timing_summary -ep_num 30 -file independent_timing.rpt
check_timing -verbose -file independent_timing_check.rpt
report_route_status -drc
set f [open independent_timing.rpt r]
set timing [read $f]
close $f
foreach metric {SWNS STNS HWNS HTNS} {
 if {![regexp "$metric: +(-?\[0-9.\]+)ns" $timing unused value]} {error "Missing $metric"}
 if {$value < 0} {error "ECO_TIMING_REJECT $metric=$value"}
}
puts POLL_ECO_INDEPENDENT_STA_PASS
} message]} {puts stderr $message; exit 1}
exit
