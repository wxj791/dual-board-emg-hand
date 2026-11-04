"""Shared readable typography and restrained colours for the workstation."""
import itertools
import os
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont, ttk

BG = '#161D2B'
PANEL = '#222C3D'
SURFACE = '#2C384C'
PLOT = '#101723'
FG = '#EDF2FA'
MUTED = '#B4C3D8'
ACCENT = '#8BBEFF'
PRIMARY = '#285EAC'
SUCCESS = '#24765C'
DANGER = '#A74457'
BODY = 14
HEADING = 16


def family(root):
    available = set(tkfont.families(root))
    return next((name for name in ('Microsoft YaHei', '微软雅黑', 'PingFang SC', 'Heiti SC')
                 if name in available), 'TkDefaultFont')


def setup(root):
    face = family(root)
    root.ui_font = face
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont',
                 'TkCaptionFont', 'TkSmallCaptionFont', 'TkIconFont', 'TkTooltipFont'):
        tkfont.nametofont(name, root=root).configure(family=face, size=BODY)
    root.option_add('*Font', (face, BODY))
    root.option_add('*TCombobox*Listbox.font', (face, BODY))
    root.option_add('*Entry.background', PLOT)
    root.option_add('*Entry.foreground', FG)
    root.option_add('*Entry.insertBackground', FG)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=(face, BODY), background=PANEL, foreground=FG)
    style.configure('TNotebook', background=BG, borderwidth=0, tabmargins=0,
                    bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
    style.configure('TNotebook.Tab', background=SURFACE, foreground=MUTED,
                    padding=(9, 9), font=(face, BODY, 'bold'),
                    borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
    style.map('TNotebook.Tab', background=[('selected', PRIMARY)],
              foreground=[('selected', 'white')], expand=[('selected',(0,0,0,0))])
    style.configure('TCombobox', fieldbackground=PLOT, background=SURFACE,
                    foreground=FG, arrowcolor=FG, padding=5,
                    bordercolor='#43516A', lightcolor='#43516A', darkcolor='#43516A')
    style.map('TCombobox', fieldbackground=[('readonly', PLOT)],
              foreground=[('readonly', FG), ('disabled', MUTED)],
              selectbackground=[('readonly', PRIMARY)])
    style.configure('TEntry', fieldbackground=PLOT, foreground=FG, padding=5)
    style.configure('TMenubutton', background=SURFACE, foreground=FG,
                    arrowcolor=FG, padding=(10,6))
    style.configure('TProgressbar', background=ACCENT, troughcolor=PLOT,
                    borderwidth=0, thickness=10)
    style.configure('TButton', padding=(10, 6), background=SURFACE, foreground=FG)
    style.map('TButton', background=[('active', PRIMARY)])
    return face


_button_ids = itertools.count()


class ActionButton(ttk.Button):
    """Themed buttons with the classic colour API used by device callbacks."""
    def __init__(self, parent, text, command, colour, **kwargs):
        self._ui_style = ttk.Style(parent)
        self._ui_name = f'Action{next(_button_ids)}.TButton'
        anchor = kwargs.pop('anchor', 'center')
        self._ui_style.configure(self._ui_name, background=colour, foreground=FG,
            font=(family(parent), BODY), padding=(10,6), borderwidth=0,
            relief='flat', anchor=anchor)
        self._ui_style.map(self._ui_name,
            background=[('disabled', SURFACE), ('pressed', '#204F8C'), ('active', PRIMARY)],
            foreground=[('disabled', '#8D9EB5')])
        super().__init__(parent, text=text, command=command, style=self._ui_name, **kwargs)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}; cnf=None
        palette = {}
        for option,target in [('bg','background'),('fg','foreground'),('font','font'),
                              ('activebackground','background'),('activeforeground','foreground')]:
            if option in kwargs: palette[target]=kwargs.pop(option)
        if palette:self._ui_style.configure(self._ui_name, **palette)
        return super().configure(cnf, **kwargs)

    config = configure


def button(parent, text, command, *, primary=False, danger=False, **kwargs):
    colour = DANGER if danger else PRIMARY if primary else SURFACE
    return ActionButton(parent, text, command, colour, **kwargs)


def label(parent, text='', *, variable=None, size=BODY, bold=False, muted=False, **kwargs):
    return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget('bg'),
                    fg=MUTED if muted else FG,
                    font=(family(parent), size, 'bold' if bold else 'normal'), **kwargs)


def check(parent, text, variable):
    return tk.Checkbutton(parent, text=text, variable=variable, bg=parent.cget('bg'),
                          fg=FG, selectcolor=PLOT, activebackground=parent.cget('bg'),
                          activeforeground=FG, font=(family(parent), BODY),
                          bd=0, highlightthickness=0, anchor='w')


def open_path(path):
    """Open a local report/log with the platform's normal application."""
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])


def short(text, maximum=64):
    text = ' '.join(str(text).split())
    return text if len(text) <= maximum else text[:maximum-1]+'…'
