if {[catch {
cd [file normalize [file join [file dirname [info script]] a_forward_header_eco]]
import_db patched.db
report_route_status -drc
reset_path_margin -all
report_timing_summary -ep_num 30 -file independent_timing.rpt
check_timing -verbose -file independent_timing_check.rpt
set f [open independent_timing.rpt r]
set timing [read $f]
close $f
foreach metric {SWNS STNS HWNS HTNS} {
 if {![regexp "$metric: +(-?\[0-9.\]+)ns" $timing unused value]} {error "Missing $metric"}
 if {$value < 0} {error "ECO_TIMING_REJECT $metric=$value"}
}
bitgen -bit A_FORWARD_HEADER_ECO_TEST_20261102.bit
puts A_FORWARD_HEADER_BITGEN_PASS
} err]} {puts stderr $err; exit 1}
exit
