foreach command {modify_netlist_from_db eco_lock_design set_property create_cell connect_net} {
 puts "HELP_FOR $command"
 catch {$command -help} message
 puts $message
}
exit
