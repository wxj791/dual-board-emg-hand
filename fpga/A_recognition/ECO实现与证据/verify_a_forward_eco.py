from pathlib import Path
import itertools
import json
import re

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'a_forward_header_eco'

def cells(path):
    text = path.read_text()
    found = {}
    for match in re.finditer(r'^  (\w+) #\((.*?)\)\s*(\\[^\s]+|\w+)\s+\((.*?)\);', text, re.S | re.M):
        model, params, name, ports = match.groups()
        params = re.sub(r'//[^\n]*', '', params)
        ports = re.sub(r'//[^\n]*', '', ports)
        params = re.sub(r'\s+', '', params)
        ports = re.sub(r'open_n\d+', 'OPEN', ports)
        ports = re.sub(r'\s+', '', ports)
        found[name.lstrip('\\')] = (model, params, ports)
    return found

baseline = cells(WORK / 'baseline_physical.v')
candidate_path = WORK / 'patched_physical.v'
if not candidate_path.exists():
    candidate_path = WORK / 'patched_before_route.v'
candidate = cells(candidate_path)
assert baseline.keys() == candidate.keys(), 'Physical cells added/removed'
changed = [name for name in baseline if baseline[name] != candidate[name]]
expected = {
    'core/seq_filter_reg[8]_syn_4',
    'pc/parameter_bank_reg[1]_syn_4',
    'mux/result_hold_reg[116]_syn_4',
    'forwarding/ramread0_syn_6',
    'forwarding/stop_header_reg[34]_syn_4',
}
assert set(changed) == expected, changed
for name in list(expected)[:]:
    if name.startswith(('forwarding/',)):
        continue
    old = baseline[name][1]
    new = candidate[name][1]
    assert re.sub(r'\.INIT_LUT1\([^)]*\),?', '', new) == old, (name, old, new)

def lut(name, a, b, c, d=0):
    params = candidate[name][1]
    value = re.search(r'\.INIT_LUT1\(16\x27b([01]{16})\)', params)
    assert value, params
    return (int(value[1], 2) >> (a + 2*b + 4*c + 8*d)) & 1

checks = 0
for wr, header, ram in itertools.product(range(64), range(2), range(2)):
    low = lut('core/seq_filter_reg[8]_syn_4', wr & 1, (wr >> 1) & 1, (wr >> 2) & 1)
    high = lut('pc/parameter_bank_reg[1]_syn_4', (wr >> 3) & 1, (wr >> 4) & 1, (wr >> 5) & 1)
    result = lut('mux/result_hold_reg[116]_syn_4', low, high, header, ram)
    assert result == (header if wr == 4 else ram), (wr, header, ram, result)
    checks += 1

report = dict(candidate=str(candidate_path), physical_cells=len(baseline),
              added_cells=0, removed_cells=0, changed_cells=changed,
              truth_table_checks=checks, new_registers=0,
              status='PASS', hardware_confirmed=False)
(WORK / 'netlist_verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
