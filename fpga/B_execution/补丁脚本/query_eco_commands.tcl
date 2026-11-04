foreach pattern {*eco* *increment* *connect* *property* *cell* *netlist* *param* *replace*} {
 puts "AVAILABLE $pattern [lsort [info commands $pattern]]"
}
exit
