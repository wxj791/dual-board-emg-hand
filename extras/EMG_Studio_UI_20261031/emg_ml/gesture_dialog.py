"""Picture-based gesture selection and a novice-facing control flow diagram."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gesture_config import (ACTION_LIBRARY, FOREARM_SCREENING_ACTIONS, GROUPS, MAX_ACTIONS, OUTPUT_ACTIONS,
                            PRESETS, PRESET_DESCRIPTIONS, action, photo_image,
                            preset, validate_profile, filter_supported_profile, is_selectable_action)

from ui_style import (BG, PANEL, SURFACE as CARD, FG, MUTED, ACCENT as BLUE,
                      button, label, family)
GREEN = '#92D8C2'


class GestureProfileDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('动作图谱 · 肌电仿生手')
        self.geometry(f'{min(1400, self.winfo_screenwidth()-40)}x{min(850, self.winfo_screenheight()-85)}')
        self.minsize(1180, 700)
        self.configure(bg=BG)
        self.transient(app)
        self.rows = []
        self.name = tk.StringVar(value=app.gesture_profile['name'])
        self.preset_name = tk.StringVar(value=app.gesture_profile['name'])
        self.count = tk.IntVar(value=len(app.gesture_profile['classes'])-1)
        self.summary = tk.StringVar()
        self.search = tk.StringVar()
        self.category = tk.StringVar(value='全部')
        self.detail_name = tk.StringVar()
        self.detail_instruction = tk.StringVar()
        self.detail_output = tk.StringVar()
        self._photos = {}
        self._columns = 3
        self._page_rows = 2
        self.page = 0
        self.page_text = tk.StringVar()
        self._focused = None
        self._selection_rows = []
        self._build()
        self.load(app.gesture_profile)
        self.search.trace_add('write', self.search_changed)
        self.grab_set()

    def _build(self):
        top = tk.Frame(self, bg=BG, padx=16, pady=12)
        top.pack(fill='x')
        label(top, '选择训练动作', size=22, bold=True).pack(side='left')
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label='打开方案', command=self.import_file)
        menu.add_command(label='保存方案', command=self.export_file)
        ttk.Menubutton(top, text='方案文件', menu=menu).pack(side='right')
        self.file_menu = menu
        bottom = tk.Frame(self, bg=BG, padx=16, pady=12)
        bottom.pack(fill='x', side='bottom')
        label(bottom, variable=self.summary, bold=True).pack(side='left')
        button(bottom, '添加动作', self.add_custom_action).pack(side='left', padx=12)
        button(bottom, '清空选择', self.clear_selection).pack(side='left')
        button(bottom, '取消', self.destroy).pack(side='right', padx=(8,0))
        button(bottom, '确认动作', self.apply, primary=True).pack(side='right')
        body = tk.Frame(self, bg=BG)
        body.pack(fill='both', expand=True, padx=16, pady=(0,4))
        body.columnconfigure(0, minsize=155)
        body.columnconfigure(1, weight=1)
        body.columnconfigure(2, minsize=248)
        body.rowconfigure(0, weight=1)
        left = tk.Frame(body, bg=PANEL, width=155)
        left.grid(row=0, column=0, sticky='nsew', padx=(0,10))
        left.pack_propagate(False)
        label(left, '动作分类', size=16, bold=True).pack(anchor='w', padx=12, pady=14)
        self.category_buttons = {}
        for name in ['全部', *GROUPS, '本轮已选', '我的动作']:
            control = button(left, name, lambda n=name: self.choose_category(n), anchor='w')
            control.pack(fill='x', padx=8, pady=4)
            self.category_buttons[name] = control
        middle = tk.Frame(body, bg=PANEL)
        middle.grid(row=0, column=1, sticky='nsew')
        tools = tk.Frame(middle, bg=PANEL, padx=10, pady=10)
        tools.pack(fill='x')
        label(tools, '预设').grid(row=0, column=0, padx=(0,8), pady=(0,8))
        self.preset_combo = ttk.Combobox(tools, textvariable=self.preset_name,
                                         values=list(PRESETS), state='readonly')
        self.preset_combo.grid(row=0, column=1, sticky='ew', pady=(0,8))
        self.preset_combo.bind('<<ComboboxSelected>>', lambda e: self.use_preset())
        label(tools, '搜索').grid(row=1, column=0, padx=(0,8))
        ttk.Entry(tools, textvariable=self.search).grid(row=1,column=1,sticky='ew')
        tools.columnconfigure(1,weight=1)
        navigation = tk.Frame(middle, bg=PANEL, padx=10, pady=8)
        navigation.pack(side='bottom',fill='x')
        self.prev_button = button(navigation,'上一页',lambda:self.change_page(-1))
        self.prev_button.pack(side='left')
        label(navigation, variable=self.page_text).pack(side='left',expand=True)
        self.next_button = button(navigation,'下一页',lambda:self.change_page(1))
        self.next_button.pack(side='right')
        self.gallery = tk.Frame(middle,bg=PANEL)
        self.gallery.pack(fill='both',expand=True,padx=6)
        self.gallery.grid_propagate(False)
        self.gallery.bind('<Configure>',self.gallery_resized)
        right = tk.Frame(body,bg=PANEL,width=248)
        right.grid(row=0,column=2,sticky='nsew',padx=(10,0))
        right.pack_propagate(False)
        label(right,'动作详情',size=16,bold=True).pack(anchor='w',padx=14,pady=14)
        self.detail_picture = tk.Label(right,bg=PANEL,height=1)
        self.detail_picture.pack(pady=8)
        label(right,variable=self.detail_name,size=20,bold=True,
              wraplength=222).pack(padx=10,pady=8)
        label(right,variable=self.detail_instruction,wraplength=216,justify='left',
              anchor='w').pack(fill='x',padx=16,pady=12)
        label(right,'机械手响应',bold=True).pack(anchor='w',padx=16,pady=(12,8))
        self.output_combo = ttk.Combobox(right,textvariable=self.detail_output,
            values=list(OUTPUT_ACTIONS.values()),state='readonly',width=14)
        self.output_combo.pack(fill='x',padx=16)
        self.output_combo.bind('<<ComboboxSelected>>',self.change_output)







    def load(self, profile):
        profile = filter_supported_profile(profile, fallback=False)
        self.rows = []
        self.name.set(profile['name'])
        self.preset_name.set(profile['name'])
        for item in profile['classes']:
            self.add_row(item)
        self.page = 0
        self.search.set('')
        self.update_summary()
        self.render_gallery()
        self.focus_row(self.rows[1] if len(self.rows)>1 else self.rows[0])

    def add_row(self, item):
        row = {'data': copy.deepcopy(item), 'enabled': tk.BooleanVar(value=True),
               'name': tk.StringVar(value=item['name']), 'instruction': tk.StringVar(value=item['instruction']),
               'output': tk.StringVar(value=f'{item["output"]["id"]}: {item["output"]["name"]}'),
               'photo': self.get_photo(item, 100)}
        self.rows.append(row)
        return row

    def get_photo(self, item, size):
        identity = (str(item.get('image')), size)
        if identity not in self._photos:
            self._photos[identity] = photo_image(item, size, self)
        return self._photos[identity]

    def choose_preset(self, name):
        self.preset_name.set(name)
        self.use_preset()

    def use_preset(self):
        self.load(preset(self.preset_name.get()))

    def choose_category(self, name):
        self.category.set(name)
        self.page = 0
        self.render_gallery()

    def search_changed(self, *args):
        self.page = 0
        self.render_gallery()

    def change_page(self, delta):
        self.page = max(0, min(self._page_count-1, self.page+delta))
        self.render_gallery()

    def gallery_resized(self, event):
        columns = max(2,min(4,event.width//195))
        rows = max(1,min(3,event.height//196))
        if (columns,rows)!=(self._columns,self._page_rows):
            self._columns,self._page_rows=columns,rows
            self.page = 0
            self.render_gallery()

    def catalog(self):
        entries = []
        represented = set()
        for keys in GROUPS.values():
            for key in keys:
                row = next((r for r in self.rows if r['data']['key'] == key), None)
                item = copy.deepcopy(row['data']) if row else action(key, 0)
                if row:
                    item.update(name=row['name'].get(), instruction=row['instruction'].get())
                    represented.add(id(row))
                entries.append((key, item, row))
        for index, row in enumerate(self.rows):
            if row['data']['key'] != 'rest' and id(row) not in represented:
                item = copy.deepcopy(row['data'])
                item.update(name=row['name'].get(), instruction=row['instruction'].get())
                entries.append((f'my:{index}', item, row))
        priority = {key: i for i, key in enumerate(FOREARM_SCREENING_ACTIONS)}
        return sorted((e for e in entries if is_selectable_action(e[1])),
                      key=lambda entry: priority.get(entry[0], len(priority)))

    def render_gallery(self):
        if not hasattr(self,'gallery'):
            return
        for child in self.gallery.winfo_children():
            child.destroy()
        category = self.category.get()
        query = self.search.get().strip().casefold()
        entries = self.catalog()
        if category in GROUPS:
            entries=[e for e in entries if e[0] in GROUPS[category]]
        elif category=='我的动作':
            entries=[e for e in entries if e[0].startswith('my:')]
        elif category=='本轮已选':
            entries=[e for e in entries if e[2] and e[2]['enabled'].get()]
        entries=[e for e in entries if not query or query in (e[1]['name']+' '+e[1]['instruction']).casefold()]
        capacity=self._columns*self._page_rows
        self._page_count=max(1,(len(entries)+capacity-1)//capacity)
        self.page=min(self.page,self._page_count-1)
        self.page_text.set(f'{self.page+1} / {self._page_count}')
        self.prev_button.configure(state='normal' if self.page else 'disabled')
        self.next_button.configure(state='normal' if self.page+1<self._page_count else 'disabled')
        self.visible_entries=entries[self.page*capacity:(self.page+1)*capacity]
        for col in range(4):
            self.gallery.columnconfigure(col,weight=1 if col<self._columns else 0,
                uniform='cards' if col<self._columns else '')
        for row in range(3):
            self.gallery.rowconfigure(row,weight=1 if row<self._page_rows else 0,
                uniform='cards' if row<self._page_rows else '')
        for i,(key,item,own_row) in enumerate(self.visible_entries):
            selected=bool(own_row and own_row['enabled'].get())
            border=tk.Frame(self.gallery,bg=GREEN if selected else '#42516A',padx=2,pady=2)
            border.grid(row=i//self._columns,column=i%self._columns,padx=5,pady=5,sticky='nsew')
            box=tk.Frame(border,bg='#28463F' if selected else CARD)
            box.pack(fill='both',expand=True)
            box.pack_propagate(False)
            photo=self.get_photo(item,96)
            image=tk.Label(box,image=photo or '',bg=box.cget('bg'),height=98 if photo else 4)
            image.pack(pady=(6,2))
            title=label(box,item['name'],bold=True,wraplength=max(155,self.gallery.winfo_width()//self._columns-26))
            title.pack(fill='x',padx=4)
            state=label(box,'✓ 已选择' if selected else '点击选择',muted=not selected)
            state.pack(pady=(2,5))
            def activate(event=None,k=key,r=own_row):
                self.toggle_action(k,r)
            for widget in (border,box,image,title,state):
                widget.bind('<Button-1>',activate)
        if not entries:
            label(self.gallery,'没有符合条件的动作',muted=True).grid(row=0,column=0,columnspan=self._columns)
        for name,control in self.category_buttons.items():
            control.configure(bg='#365B78' if name==category else CARD,
                              fg=FG if name==category else MUTED)

    def toggle_action(self, key, row=None):
        row = row or next((r for r in self.rows if r['data']['key'] == key), None)
        if key in {'supination','pronation','supination_ulnar','pronation_ulnar'}:
            return
        if not is_selectable_action(row['data'] if row else action(key, 0)):
            return
        if row is not None and row['enabled'].get():
            row['enabled'].set(False)
        else:
            n = sum(r['enabled'].get() for r in self.rows[1:])
            if n >= MAX_ACTIONS:
                messagebox.showinfo('本轮动作已满', f'本轮最多选择{MAX_ACTIONS}个动作。', parent=self)
                return
            if row is None:
                row = self.add_row(action(key, len(self.rows)))
            else:
                row['enabled'].set(True)
        self.name.set('我的动作方案')
        self.preset_name.set('')
        self.update_summary()
        self.render_gallery()
        self.focus_row(row)

    def focus_row(self, row):
        self._focused = row
        item = row['data']
        self._detail_photo = self.get_photo(item, 140)
        self.detail_picture.configure(image=self._detail_photo or '', height=145)
        self.detail_name.set(row['name'].get())
        self.detail_instruction.set(row['instruction'].get())
        name = row['output'].get().partition(':')[2].strip()
        self.detail_output.set(name)
        values = list(OUTPUT_ACTIONS.values())
        if name not in values:
            values.append(name)
        self.output_combo.configure(values=values, state='disabled' if item['key'] == 'rest' else 'readonly')



    def change_output(self, event=None):
        if self._focused is None:
            return
        name = self.detail_output.get()
        code = next((i for i, label in OUTPUT_ACTIONS.items() if label == name), None)
        if code is not None:
            self._focused['output'].set(f'{code}: {name}')

    def update_summary(self):
        self._selection_rows=[r for r in self.rows if r['enabled'].get()]
        n=max(0,len(self._selection_rows)-1)
        self.count.set(n)
        self.summary.set(f'已选 {n} 个动作＋静息')

    def collect(self, selected_only=True):
        classes = []
        for row in self.rows:
            if selected_only and not row['enabled'].get():
                continue
            item = copy.deepcopy(row['data'])
            item.update(id=len(classes), name=row['name'].get(), instruction=row['instruction'].get())
            code, sep, name = row['output'].get().partition(':')
            if not sep or not code.strip().isdigit():
                raise ValueError('请为每个动作选择机械手响应。')
            item['output'] = {'id': int(code.strip()), 'name': name.strip()}
            classes.append(item)
        return {'version': 1, 'name': self.name.get(), 'classes': classes}

    def clear_selection(self):
        self.load({'name': '我的动作方案', 'classes': [action('rest', 0)]})

    def resize(self):
        count = int(self.count.get())
        if not 1 <= count <= MAX_ACTIONS:
            raise ValueError(f'请选择1～{MAX_ACTIONS}个动作。')
        profile = self.collect()
        profile['classes'] = profile['classes'][:count+1]
        while len(profile['classes']) < count+1:
            i = len(profile['classes'])
            item = action('custom', i)
            item['name'] = f'我的动作{i}'
            profile['classes'].append(item)
        self.load(profile)

    def add_custom_action(self):
        if sum(r['enabled'].get() for r in self.rows[1:]) >= MAX_ACTIONS:
            messagebox.showinfo('本轮动作已满', f'本轮最多选择{MAX_ACTIONS}个动作。', parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title('我的动作')
        dialog.geometry('600x400')
        dialog.configure(bg=PANEL)
        dialog.transient(self)
        dialog.grab_set()
        name = tk.StringVar()
        instruction = tk.StringVar()
        image = tk.StringVar(value='custom.png')
        for title, var in [('动作名称', name), ('动作要领', instruction)]:
            tk.Label(dialog, text=title, bg=PANEL, fg=FG, anchor='w').pack(fill='x', padx=20, pady=(15, 5))
            ttk.Entry(dialog, textvariable=var).pack(fill='x', padx=20)
        def choose():
            path = filedialog.askopenfilename(parent=dialog, filetypes=[('动作图片', '*.png *.jpg *.jpeg *.webp')])
            if path:
                image.set(path)
        ttk.Button(dialog, text='选择动作图片', command=choose).pack(pady=15)
        def save():
            if not name.get().strip() or not instruction.get().strip():
                messagebox.showinfo('动作还未完成', '请输入动作名称和动作要领。', parent=dialog)
                return
            item = action('custom', len(self.rows))
            item.update(name=name.get().strip(), instruction=instruction.get().strip(), image=image.get())
            if not is_selectable_action(item):
                messagebox.showinfo('请选择手指或手腕动作', '不支持前臂翻转或空间动作。', parent=dialog)
                return
            try:
                row = self.add_row(item)
            except Exception as exc:
                messagebox.showerror('图片无法打开', str(exc), parent=dialog)
                return
            self.name.set('我的动作方案')
            self.preset_name.set('')
            self.category.set('我的动作')
            self.update_summary()
            self.render_gallery()
            self.focus_row(row)
            dialog.destroy()
            self.grab_set()
        button(dialog, '加入本轮', save, primary=True).pack()
        dialog.protocol('WM_DELETE_WINDOW', lambda: (dialog.destroy(), self.grab_set()))

    def import_file(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[('动作方案', '*.json')])
        if path:
            try:
                profile = json.loads(Path(path).read_text(encoding='utf-8'))
                for row in profile.get('classes', []):
                    image = Path(row.get('image', 'custom.png'))
                    if not image.is_absolute() and (Path(path).parent/image).is_file():
                        row['image'] = str((Path(path).parent/image).resolve())
                self.load(validate_profile(profile))
            except Exception as exc:
                messagebox.showerror('方案无法打开', str(exc), parent=self)

    def export_file(self):
        try:
            profile = validate_profile(self.collect())
        except ValueError as exc:
            messagebox.showinfo('请先选好动作', str(exc), parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.json', filetypes=[('动作方案', '*.json')])
        if path:
            try:
                Path(path).write_text(json.dumps(profile, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
            except OSError as exc:
                messagebox.showerror('保存失败', str(exc), parent=self)

    def apply(self):
        try:
            self.app.apply_gesture_profile(validate_profile(self.collect()), persist=True)
        except (ValueError, OSError) as exc:
            messagebox.showinfo('请先选好动作', str(exc), parent=self)
            return
        self.app.notebook.select(1)
        self.destroy()
