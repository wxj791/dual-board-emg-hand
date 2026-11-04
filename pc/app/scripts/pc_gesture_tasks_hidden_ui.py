"""Command-only Tk regression: all root/toplevel windows remain withdrawn."""
from pathlib import Path
import os
import sys
import tkinter as tk
import unittest
from unittest.mock import patch
import serial
from serial.tools import list_ports
ROOT=Path(__file__).resolve().parents[1]
EMG=ROOT/'EMG_Studio/EMG_Studio/emg_ml'
sys.path.insert(0,str(EMG));sys.path.insert(0,str(EMG.parent/'backend'))
os.environ['EMG_FPGA_PROJECT']=str(EMG.parent/'backend')

def hidden_init(original):
    def initialize(self,*args,**kwargs):
        original(self,*args,**kwargs);self.withdraw()
    return initialize

if __name__=='__main__':
    with patch.object(tk.Tk,'__init__',hidden_init(tk.Tk.__init__)), \
         patch.object(tk.Toplevel,'__init__',hidden_init(tk.Toplevel.__init__)), \
         patch.object(tk.Wm,'deiconify',lambda self:None), \
         patch.object(serial,'Serial',side_effect=AssertionError('offline checks cannot open serial')), \
         patch.object(list_ports,'comports',return_value=[]):
        suite=unittest.defaultTestLoader.discover(str(EMG/'tests'),pattern='test_field_integration.py')
        # These assertions specifically require mapped desktop windows. Report
        # them as skipped, never as hidden-window layout verification.
        visual={'test_collection_guide_follows_collection_across_tabs',
                'test_compact_pages_keep_active_controls_visible_without_scrollbars',
                'test_removed_training_block_is_not_visible'}
        def mark_visual(items):
            for item in items:
                if isinstance(item,unittest.TestSuite):mark_visual(item)
                elif item._testMethodName in visual:
                    method=getattr(type(item),item._testMethodName)
                    setattr(type(item),item._testMethodName,unittest.skip('desktop mapping disabled at user request')(method))
        mark_visual(suite)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(not result.wasSuccessful())
