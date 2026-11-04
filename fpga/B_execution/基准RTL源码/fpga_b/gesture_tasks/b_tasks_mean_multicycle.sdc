# The scanner selects a lane in state 3/6 and writes a sum in state 6.
# Sum-dependent data and the lane selector are stable for at least 3 clocks.
# Pair data and every other pixel path retain ordinary single-cycle checks.
set_multicycle_path -setup 3 -from [get_regs {renderer/mean_lane* renderer/mean_sum*}] -to [get_regs {renderer/mean_sum*}]
set_multicycle_path -hold 2 -from [get_regs {renderer/mean_lane* renderer/mean_sum*}] -to [get_regs {renderer/mean_sum*}]
