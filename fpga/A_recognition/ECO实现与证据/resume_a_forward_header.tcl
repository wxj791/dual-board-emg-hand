if {[catch {
cd [file normalize [file join [file dirname [info script]] a_forward_header_eco]]
import_db patched_before_route.db
route -eco
report_route_status -drc
reset_path_margin -all
report_timing_summary -ep_num 30 -file final_timing.rpt
check_timing -verbose -file timing_check.rpt
set f [open final_timing.rpt r]
set timing [read $f]
close $f
foreach metric {SWNS STNS HWNS HTNS} {
 if {![regexp "$metric: +(-?\[0-9.\]+)ns" $timing unused value]} {error "Missing $metric"}
 if {$value < 0} {error "ECO_TIMING_REJECT $metric=$value"}
}
write_verilog patched_physical.v
export_db patched.db
puts A_FORWARD_ECO_ROUTED_PASS
} err]} {puts stderr $err; exit 1}
exit
