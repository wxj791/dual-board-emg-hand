create_clock -name clk_24m -period 35.0 [get_ports {clk_24m}]
create_generated_clock -name link_clock_out -source [get_ports {clk_24m}] -divide_by 24 [get_ports {link_clk}]
# B changes READY after a link rising edge. A uses it at the next rising edge.
set_input_delay -clock link_clock_out -max 15 [get_ports {link_ready}]
set_input_delay -clock link_clock_out -min 5 [get_ports {link_ready}]
set_output_delay -clock link_clock_out -max 15 [get_ports {link_data[*] link_valid}]
set_output_delay -clock link_clock_out -min -1 [get_ports {link_data[*] link_valid}]
set_output_delay -clock clk_24m -max 5 [get_ports {pc_uart_tx status_led_n[*]}]
set_output_delay -clock clk_24m -min 0 [get_ports {pc_uart_tx status_led_n[*]}]
# Asynchronous external reset: local deassertion goes through reset_sync_n.
set_false_path -from [get_ports {rst_n}]
# UART pin only drives ingress RX meta synchronizer; meta->sync stays timed.
# In ROM mode the unused ingress is pruned.
set_false_path -from [get_ports {pc_uart_rx}]

set_false_path -from [get_ports {wireless_uart_rx}]

set_false_path -from [get_ports {b_return_rx}]
