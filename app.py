"""Local desktop setup and lifecycle UI (Tkinter, no web server)."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from local_config import ROOT, read_config
import local_control


class App:
    def __init__(self, window):
        self.window = window
        self.results = queue.Queue()
        self.busy = False
        window.title('群友 · 本地分享版 0.1')
        window.geometry('920x790')
        window.minsize(760, 680)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TLabel', font=('Microsoft YaHei UI', 10))
        style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=7)
        style.configure('TCheckbutton', font=('Microsoft YaHei UI', 10), wraplength=810)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        frame.bind('<Configure>', lambda event: style.configure('TCheckbutton', wraplength=max(400, event.width - 70)))
        ttk.Label(frame, text='你的 GPT，你的微信', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='本机运行 · 单群接入 · 使用你自己登录的 Codex / ChatGPT 账号').pack(anchor='w', pady=(2, 14))
        tabs = ttk.Notebook(frame)
        tabs.pack(fill='both', expand=True)
        setup = ttk.Frame(tabs, padding=14)
        role = ttk.Frame(tabs, padding=14)
        console = ttk.Frame(tabs, padding=14)
        tabs.add(setup, text='1  微信与账号')
        tabs.add(role, text='2  人设与模型')
        tabs.add(console, text='3  检测与日志')
        setup.columnconfigure(1, weight=1)
        role.columnconfigure(1, weight=1)
        self.vars = {}
        config = read_config()

        def entry(parent, row, key, title):
            ttk.Label(parent, text=title).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=7)
            self.vars[key] = tk.StringVar(value=str(config[key]))
            ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky='ew', pady=7)

        entry(setup, 0, 'group', '微信群完整名称')
        entry(setup, 1, 'self_name', '本人在群里的显示昵称')
        ttk.Label(setup, text='全体成员显示昵称（每行一人，包含自己；按聊天多选界面显示填写）', wraplength=700).grid(row=2, columnspan=2, sticky='w', pady=(12, 6))
        self.members = tk.Text(setup, height=11, font=('Microsoft YaHei UI', 10), undo=True)
        self.members.grid(row=3, columnspan=2, sticky='nsew')
        setup.rowconfigure(3, weight=1)
        self.members.insert('1.0', '\n'.join(config['members']))
        ttk.Label(setup, text='先在电脑微信中手动登录并切到目标群。群名应唯一；成员加入、退出或改名后需停机更新名单。', wraplength=720).grid(row=4, columnspan=2, sticky='w', pady=9)
        ttk.Button(setup, text='登录自己的 GPT（官方登录窗口）', command=self.login).grid(row=5, columnspan=2, sticky='w', pady=4)
        entry(role, 0, 'nickname', '机器人名字')
        entry(role, 1, 'model', '模型（留空用 CLI 默认）')
        entry(role, 2, 'context_messages', '上下文消息数（1–60）')
        ttk.Label(role, text='推理档位').grid(row=3, column=0, sticky='w')
        self.vars['reasoning_effort'] = tk.StringVar(value=config['reasoning_effort'])
        ttk.Combobox(role, textvariable=self.vars['reasoning_effort'], values=['low', 'medium', 'high'], state='readonly').grid(row=3, column=1, sticky='ew')
        self.texts = {}
        for row, key, title in [(4, 'character', '角色设定'), (5, 'style', '说话风格'), (6, 'participation', '什么时候参与聊天')]:
            ttk.Label(role, text=title).grid(row=row, column=0, sticky='nw', pady=9)
            widget = tk.Text(role, height=3, font=('Microsoft YaHei UI', 10), wrap='word', undo=True)
            widget.grid(row=row, column=1, sticky='nsew', pady=5)
            widget.insert('1.0', config[key])
            self.texts[key] = widget
            role.rowconfigure(row, weight=1)
        self.stickers = tk.BooleanVar(value=config['stickers'])
        ttk.Checkbutton(role, text='启用表情收藏与表情回复（会截取目标消息、操作微信收藏）', variable=self.stickers).grid(row=7, columnspan=2, sticky='w', pady=6)
        buttons = ttk.Frame(console)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='环境与微信检测', command=lambda: self.command('doctor', '--wechat')).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='测试 GPT（不发微信）', command=lambda: self.command('probe')).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='打开本地日志', command=self.open_log).pack(side='left')
        self.output = tk.Text(console, height=15, wrap='word', font=('Microsoft YaHei UI', 10), state='disabled')
        self.output.pack(fill='both', expand=True, pady=10)
        self.log('使用顺序：填写配置 → 保存 → 登录 GPT → 环境检测 → 测试 GPT → 启动。\n检测与测试结果显示在这里。测试 GPT 会使用你自己的账户额度。')
        self.consent = tk.BooleanVar(value=config['consent'])
        ttk.Checkbutton(frame, text='我使用自己的账号；同意指定群昵称、消息和上下文发给自己的 GPT。表情消息会发送该条截图。', variable=self.consent).pack(anchor='w', pady=(12, 4))
        ttk.Label(frame, text='微信需登录且桌面解锁。发送时会短暂操作本机微信；切到其他聊天或有未发草稿时等待。', wraplength=850).pack(anchor='w', pady=4)
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(8, 4))
        ttk.Button(actions, text='保存配置', command=self.save).pack(side='left')
        ttk.Button(actions, text='启动群友', command=self.start).pack(side='left', padx=8)
        ttk.Button(actions, text='停止群友', command=self.stop).pack(side='left')
        self.state = tk.StringVar(value='已停止')
        ttk.Label(actions, textvariable=self.state, wraplength=470).pack(side='right')
        ttk.Label(frame, text='关闭此窗口不停止群友；请使用“停止群友”或 03-stop.cmd。', foreground='#666666').pack(anchor='w', pady=4)
        window.after(250, self.drain)
        window.after(500, self.refresh)

    def log(self, text):
        self.output.configure(state='normal')
        self.output.insert('end', str(text) + '\n\n')
        self.output.see('end')
        self.output.configure(state='disabled')

    def data(self):
        data = {k: v.get() for k, v in self.vars.items()}
        data['context_messages'] = int(data['context_messages'])
        data.update({k: v.get('1.0', 'end').strip() for k, v in self.texts.items()})
        data.update(members=[n.strip() for n in self.members.get('1.0', 'end').splitlines() if n.strip()],
                    stickers=self.stickers.get(), consent=self.consent.get())
        return data

    def save(self):
        if self.busy:
            messagebox.showinfo('正在处理', '请等待当前操作结束。')
            return False
        try:
            local_control.save(self.data())
            self.log('配置已保存。更换目标群或名单时，旧队列会归档到 local_data。')
            return True
        except Exception as exc:
            messagebox.showerror('配置未保存', str(exc))
            return False

    def background(self, function):
        if self.busy:
            messagebox.showinfo('正在处理', '请等待当前操作结束。')
            return
        self.busy = True
        def work():
            try:
                self.results.put(function())
            except Exception as exc:
                self.results.put('操作失败：' + str(exc))
        threading.Thread(target=work, daemon=True).start()

    def command(self, *args):
        if self.save():
            def run():
                result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'local_control.py'), *args],
                    cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace', timeout=155,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                return result.stdout + result.stderr
            self.log('正在执行：' + ' '.join(args))
            self.background(run)

    def start(self):
        if self.save():
            self.background(lambda: json.dumps(local_control.start(), ensure_ascii=False, indent=2))

    def stop(self):
        try:
            local_control.stop()
            self.log('已请求停止。正在发送的一条可能已完成；后续消息不再发送。')
        except Exception as exc:
            self.log(str(exc))

    def login(self):
        try:
            from local_codex import resolve_codex_executable
            subprocess.Popen([resolve_codex_executable(), 'login'], cwd=ROOT,
                creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
            self.log('请在官方浏览器页面中登录自己的账号；完成后运行环境检测。')
        except Exception as exc:
            messagebox.showerror('无法登录', str(exc))

    def open_log(self):
        path = ROOT / '.runtime/bot.log'
        if path.exists():
            os.startfile(str(path))
        else:
            self.log('还没有运行日志。')

    def drain(self):
        try:
            while True:
                self.log(self.results.get_nowait())
                self.busy = False
        except queue.Empty:
            pass
        self.window.after(250, self.drain)

    def refresh(self):
        try:
            info = local_control.status()
            labels = {'stopped': '已停止', 'starting': '启动中', 'listening': '监听中',
                      'waiting': '等待微信就绪', 'paused_generation': 'GPT 生成已暂停',
                      'paused_unknown_delivery': '发送结果不明，已暂停；请核对微信群与日志'}
            text = labels.get(info.get('state'), info.get('state', '启动中'))
            if info.get('process_running') and (info.get('error') or info.get('generation_error')):
                text += ' · ' + str(info.get('generation_error') or info['error'])[:100]
            self.state.set(text)
        except Exception as exc:
            self.state.set('状态读取失败：' + str(exc)[:70])
        self.window.after(1500, self.refresh)


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
