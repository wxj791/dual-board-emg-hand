"""Gesture-task candidate launcher. Opens no port until the user connects."""
import os
import argparse
import hashlib
from pathlib import Path
import runpy
from start_emg_studio import configure

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-package',type=Path)
    protocol=parser.add_mutually_exclusive_group()
    protocol.add_argument('--legacy-opcodes',action='store_true',
                          help='Use the original A/B firmware (default).')
    protocol.add_argument('--sparse-opcodes',action='store_true',
                          help='Experimental: original A with sparse-opcode B firmware.')
    args=parser.parse_args()
    # Restore the original A/B protocol for every normal entry. A stale
    # inherited environment must not silently re-enable the experiment.
    os.environ['EMG_SPARSE_OPCODES']='1' if args.sparse_opcodes else '0'
    selected=args.model_package.resolve() if args.model_package else None
    configure()
    if selected:
        from gesture_tasks_materials import require_export_inputs
        require_export_inputs(selected)
        identity=hashlib.sha256((selected/'parameters.bin').read_bytes()).hexdigest()[:16]
        os.environ['EMG_FIELD_DEFAULT_PACKAGE']=str(selected)
        os.environ['EMG_FIELD_SETTINGS_FILE']=str(Path(__file__).resolve().parent/'runtime'/f'model_{identity}_selection.json')
    os.environ['EMG_BOARD_PROTOCOL']='1'
    runpy.run_path(str(Path(__file__).resolve().parent/'emg_ml/emg_studio_gui_V3.0.py'),run_name='__main__')
