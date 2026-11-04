create_clock -name clk_24m -period 41.666667 [get_ports {clk_24m}]
create_clock -name link_clk -period 166.666667 [get_ports {link_clk}]
derive_clocks
rename_clock -name clk_pix [get_clocks {u_pll/pll.clkc[0]}]
rename_clock -name clk_dac [get_clocks {u_pll/pll.clkc[1]}]
create_generated_clock -name dac_clock -source [get_pins {u_pll/pll.clkc[1]}] -divide_by 1 [get_ports {vga_clk}]
set_clock_uncertainty -setup 0.15 [get_clocks {clk_pix clk_dac dac_clock}]
set_clock_uncertainty -hold 0.10 [get_clocks {clk_pix clk_dac dac_clock}]
set_output_delay -clock dac_clock -reference_pin [get_ports {vga_clk}] -max 0.7 [get_ports {vga_r[*] vga_g[*] vga_b[*]}]
set_output_delay -clock dac_clock -reference_pin [get_ports {vga_clk}] -min -2.0 [get_ports {vga_r[*] vga_g[*] vga_b[*]}]
set_output_delay -clock dac_clock -reference_pin [get_ports {vga_clk}] -max 0.7 [get_ports {vga_hs vga_vs}]
set_output_delay -clock dac_clock -reference_pin [get_ports {vga_clk}] -min 0 [get_ports {vga_hs vga_vs}]
set_output_delay -clock clk_24m -max 5 [get_ports {host_uart_tx b_return_tx scs_tx flash_cs_n flash_clk flash_mosi status_led_n[*]}]
set_output_delay -clock clk_24m -min 0 [get_ports {host_uart_tx b_return_tx scs_tx flash_cs_n flash_clk flash_mosi status_led_n[*]}]
set_input_delay -clock clk_24m -max 10 [get_ports {flash_miso}]
set_input_delay -clock clk_24m -min 0 [get_ports {flash_miso}]
set_false_path -from [get_ports {rst_n host_uart_rx scs_rx display_sw}]
set_false_path -from [get_clocks {clk_24m}] -to [get_regs {pix_sync/sync_pipe[*]}]
# CDC mailbox synchronizer first stages are intentionally asynchronous.
# The bundled payload is covered separately by the max-delay constraint below.
set_false_path -to [get_regs {*req_s1 *ack_s1}]
# link wrapper error toggle crosses only through its two-flop synchronizer.
set_false_path -from [get_regs {receiver/overflow_toggle}] -to [get_regs {receiver/overflow_sync1}]
set_max_delay -from [get_regs {meta_mailbox/payload_hold[*]}] -to [get_regs {meta_mailbox/dst_data[*]}] -datapath_only 6.734007
set_max_delay -from [get_regs {sample_fifo/wr_gray[*]}] -to [get_regs {sample_fifo/wr_gray_rd_sync1[*]}] -datapath_only 41.666667
set_max_delay -from [get_regs {sample_fifo/rd_gray[*]}] -to [get_regs {sample_fifo/rd_gray_wr_sync1[*]}] -datapath_only 6.734007
set_input_delay -clock link_clk -clock_fall -max 5 [get_ports {link_data[*] link_valid}]
set_input_delay -clock link_clk -clock_fall -min 0 [get_ports {link_data[*] link_valid}]
set_output_delay -clock link_clk -max 5 [get_ports {link_ready}]
set_output_delay -clock link_clk -min 0 [get_ports {link_ready}]
set_false_path -from [get_clocks {clk_24m}] -to [get_regs {receiver/link_reset_sync/sync_pipe[*]}]
set_max_delay -from [get_regs {receiver/byte_fifo/wr_gray[*]}] -to [get_regs {receiver/byte_fifo/wr_gray_rd_sync1[*]}] -datapath_only 166.666667
set_max_delay -from [get_regs {receiver/byte_fifo/rd_gray[*]}] -to [get_regs {receiver/byte_fifo/rd_gray_wr_sync1[*]}] -datapath_only 41.666667
