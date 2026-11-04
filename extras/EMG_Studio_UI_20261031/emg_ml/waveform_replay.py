"""Display-only playback of recorded eight-channel signals, without devices."""
from pathlib import Path
import time
from tkinter import filedialog, messagebox

import numpy as np
from signal_processing import EMGSignalFilter, EMGEnvelope
from ui_style import ACCENT, FG


class WaveformReplay:
    def __init__(self, app):
        self.app=app
        self.active=False
        self.timer=None
        self.raw=self.filtered=self.envelope=None
        self.cursor=0

    def toggle(self):
        if self.active:
            self.stop()
            return
        if self.app.core.running or self.app.core.is_recording:
            messagebox.showinfo('正在使用设备','请先停止采集并断开串口。',parent=self.app)
            return
        path=filedialog.askopenfilename(parent=self.app,title='载入波形记录',
            initialdir=Path(__file__).resolve().parent/'data/raw',
            filetypes=[('肌电记录','*.npz')])
        if path:
            try:self.load(path)
            except (ValueError,OSError,KeyError) as exc:
                messagebox.showerror('无法回放',str(exc),parent=self.app)

    def load(self,path,offset=0):
        """Load raw saved channel order; never publish into inference/record queues."""
        app=self.app
        thread=app.paradigm_thread
        if (app.core.running or app.core.is_recording or (thread and thread.is_alive())
                or app.field_training.busy or app.field_training.live_generation is not None):
            raise ValueError('请先停止设备采集、识别或训练任务。')
        with np.load(path,allow_pickle=False) as archive:
            raw=np.asarray(archive['emg'],dtype=np.float32)
            rate=float(archive['sample_rate']) if 'sample_rate' in archive else 1000.0
        if (raw.ndim!=2 or raw.shape[1]!=8 or len(raw)<2 or not np.isfinite(raw).all()):
            raise ValueError('记录必须包含有效的八通道数据。')
        if not np.isfinite(rate) or rate<=0:
            raise ValueError('记录的采样率无效。')
        filtered=EMGSignalFilter().process(raw)
        envelope=EMGEnvelope().process(filtered)
        self.stop()
        self.raw,self.filtered,self.envelope=raw,filtered,envelope
        self.rate=rate
        self.path=Path(path)
        self.offset=max(0,min(int(offset),len(raw)-2))
        self.started=time.monotonic()
        self.active=True
        app.btn_wave_replay.configure(text='停止回放')
        app.lbl_link_status.configure(text='离线回放',fg=ACCENT)
        self._tick()

    def _render_at(self,end):
        end=max(2,min(len(self.raw),int(end)))
        start=max(0,end-500)
        count=end-start
        core=self.app.core
        with core.disp_lock:
            for buffer,signal in ((core.disp_buf,self.raw),(core.filtered_buf,self.filtered),
                                  (core.envelope_buf,self.envelope)):
                buffer.fill(0)
                buffer[-count:]=signal[start:end]
            core.disp_count=count
        self.cursor=end
        self.app.lbl_sps.configure(text=f'{end/self.rate:.1f} / {len(self.raw)/self.rate:.1f} s')

    def _tick(self):
        self.timer=None
        if not self.active:return
        end=self.offset+int((time.monotonic()-self.started)*self.rate)
        self._render_at(end)
        if end>=len(self.raw):
            self.active=False
            self.app.btn_wave_replay.configure(text='回放')
            self.app.lbl_link_status.configure(text='回放结束',fg=ACCENT)
        else:
            self.timer=self.app.after(40,self._tick)

    def stop(self):
        self.active=False
        if self.timer is not None:
            self.app.after_cancel(self.timer)
            self.timer=None
        if self.raw is not None:
            core=self.app.core
            with core.disp_lock:
                core.disp_count=0
                for buffer in (core.disp_buf,core.filtered_buf,core.envelope_buf):buffer.fill(0)
            self.raw=self.filtered=self.envelope=None
            self.app.btn_wave_replay.configure(text='回放')
            if not core.running:
                self.app.lbl_link_status.configure(text='未连接',fg=FG)
                self.app.lbl_sps.configure(text='0 点/秒')
