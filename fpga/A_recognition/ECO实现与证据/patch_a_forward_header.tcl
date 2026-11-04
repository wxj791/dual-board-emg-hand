if {[catch {
set work [file normalize [file join [file dirname [info script]] a_forward_header_eco]]
file mkdir $work
cd $work
import_db {E:/FPGA/Projects/emg_hand_fpga/artifacts/a_gesture_tasks/route32_balanced_final24_20261101_r1/routed.db}
write_verilog baseline_physical.v
eco_lock_design
# These three LUT1 halves have no LUT inputs, output, or register connected.
# Their LUT0 halves and all register/clock pins remain untouched.
set low {core/seq_filter_reg[8]_syn_4}
set high {pc/parameter_bank_reg[1]_syn_4}
set mux {mux/result_hold_reg[116]_syn_4}
foreach net {eco_header_low eco_header_high eco_forward_data0} {create_net $net}
foreach {cell expression} [list $low {=(~A*~B*C)} $high {=(~A*~B*~C)} $mux {=(A*B*C+~A*D+~B*D)}] {
 set_inst_attr -model_name v2_board_top -inst $cell -attr_key LUT1 -attr_value $expression
}
foreach {pin net} {a[1] forwarding/wr[0] b[1] forwarding/wr[1] c[1] forwarding/wr[2] f[1] eco_header_low} {
 connect_inst_pin -inst $low -pin $pin -net $net
}
foreach {pin net} {a[1] forwarding/wr[3] b[1] forwarding/wr[4] c[1] forwarding/wr[5] f[1] eco_header_high} {
 connect_inst_pin -inst $high -pin $pin -net $net
}
foreach {pin net} {a[1] eco_header_low b[1] eco_header_high c[1] pc/header[32] d[1] pc/forward_data[0] f[1] eco_forward_data0} {
 connect_inst_pin -inst $mux -pin $pin -net $net
}
disconnect_inst_pin -inst forwarding/ramread0_syn_6 -pin {dia[0]}
connect_inst_pin -inst forwarding/ramread0_syn_6 -pin {dia[0]} -net eco_forward_data0
disconnect_inst_pin -inst {forwarding/stop_header_reg[34]_syn_4} -pin {b[0]}
connect_inst_pin -inst {forwarding/stop_header_reg[34]_syn_4} -pin {b[0]} -net eco_forward_data0
write_verilog patched_before_route.v
export_db patched_before_route.db
export_db patched_before_route.db
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
