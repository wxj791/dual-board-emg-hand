foreach command {modify_netlist_from_db eco_lock_design set_property} {
 puts "QUERY $command"
 set status [catch {help $command} message]
 puts "HELP_RESULT $status <$message>"
 set status [catch {$command} message]
 puts "USAGE_RESULT $status <$message>"
 if {[llength [info procs $command]]} {
  puts "PROC_ARGS [info args $command]"
  puts "PROC_BODY [info body $command]"
 }
}
exit
