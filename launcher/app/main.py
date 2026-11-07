"""BA 扫描工具箱 —— 三引擎图形化整合启动器。

方法一：内存直读导出（BA数据导出工具 / ba-data-export）——只读取游戏进程，约 30 秒
方法二：库存 OCR 扫描（BA库存抓包 / ba-inventory-ocr）——截图识别背包
方法三：学生练度 OCR 扫描（BA练度扫描）——详情页逐个识别

引擎均为子进程调用既有脚本，本程序不重复实现扫描逻辑。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent           # launcher/app
LAUNCHER_DIR = APP_DIR.parent                        # launcher/
PROJECT_DIR = LAUNCHER_DIR.parent                    # 仓库根 / 便携解压根

ENGINE_MEMORY = "memory"
ENGINE_INVENTORY = "inventory"
ENGINE_STUDENT = "student"

DEFAULT_CONFIG = {
    "memory_tool_dir": str(PROJECT_DIR / "engines" / "data-export"),
    "memory_python": "",
    "inventory_tool_dir": str(PROJECT_DIR / "engines" / "inventory-ocr"),
    "student_tool_dir": str(PROJECT_DIR / "engines" / "student-scan"),
    "ocr_python": "",
}


def load_config():
    config = dict(DEFAULT_CONFIG)
    for candidate in (PROJECT_DIR / "config.json", APP_DIR / "config.json"):
        if candidate.is_file():
            try:
                config.update(json.loads(candidate.read_text(encoding="utf-8-sig")))
            except (OSError, json.JSONDecodeError):
                pass
            break
    return config


CONFIG = load_config()


def game_running():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq BlueArchive.exe"],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        return "BlueArchive.exe" in (result.stdout or "")
    except Exception:
        return False


class Engine:
    def __init__(self, key, title, subtitle, options, build_cmd, open_dir_fn):
        self.key = key
        self.title = title
        self.subtitle = subtitle
        self.options = options
        self.build_cmd = build_cmd
        self.open_dir_fn = open_dir_fn


def engine_memory_cmd(options):
    tool = Path(CONFIG["memory_tool_dir"])
    python = CONFIG.get("memory_python") or sys.executable
    if not (tool / "ba_data_export" / "ba_export.py").is_file():
        raise FileNotFoundError(f"未找到内存导出脚本：{tool / 'ba_data_export' / 'ba_export.py'}（检查 config.json memory_tool_dir）")
    cmd = [python, "-I", "-B", str(tool / "ba_data_export" / "ba_export.py")]
    return cmd, tool


def engine_inventory_cmd(options):
    tool = Path(CONFIG["inventory_tool_dir"])
    python = CONFIG.get("ocr_python")
    if not python or not Path(python).is_file():
        raise FileNotFoundError("未找到 OCR venv 解释器（config.json ocr_python）")
    if not (tool / "ba_queue_collector.py").is_file():
        raise FileNotFoundError(f"未找到库存采集脚本：{tool}")
    cmd = [python, "-u", "ba_queue_collector.py"]
    if options.get("fresh"):
        cmd.append("--fresh")
    return cmd, tool


def engine_student_cmd(options):
    tool = Path(CONFIG["student_tool_dir"])
    python = CONFIG.get("ocr_python")
    if not python or not Path(python).is_file():
        raise FileNotFoundError("未找到 OCR venv 解释器（config.json ocr_python）")
    script = tool / "ba_student_scan.py"
    if not script.is_file():
        raise FileNotFoundError(f"未找到练度扫描脚本：{script}")
    cmd = [python, "-I", str(script)]
    max_count = options.get("max") or 400
    cmd += ["--max", str(max_count)]
    if options.get("once"):
        cmd.append("--once")
    return cmd, tool


def open_path(path):
    if Path(path).exists():
        os.startfile(str(path))


ENGINES = [
    Engine(
        ENGINE_MEMORY, "内存直读导出（推荐）",
        "只读游戏进程 · 约 30 秒 · 最精确\n导出道具/装备/武器/角色 JSON + 什亭之匣格式（对账按其 config.json 自动进行）",
        [],
        engine_memory_cmd,
        lambda: Path(CONFIG["memory_tool_dir"]) / "输出" / "exports"),
    Engine(
        ENGINE_INVENTORY, "库存 OCR 扫描",
        "截图识别背包页 · 需游戏前台 · 期间勿动鼠标\n采集后自动映射生成什亭之匣 JSON",
        [("清空重扫（默认断点续采）", "fresh", False)],
        engine_inventory_cmd,
        lambda: Path(CONFIG["inventory_tool_dir"]) / "输出"),
    Engine(
        ENGINE_STUDENT, "学生练度 OCR 扫描",
        "详情页逐个识别 · 需游戏前台 · 自动翻页收工\n输出 TXT 总表 + JSON",
        [("仅单帧调试（不翻页）", "once", False), ("学生数量上限", "max", 400)],
        engine_student_cmd,
        lambda: Path(CONFIG["student_tool_dir"]) / "输出"),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BA 扫描工具箱 —— 蔚蓝档案数据扫描")
        self.geometry("860x640")
        self.minsize(780, 560)
        self.engine_var = tk.StringVar(value=ENGINE_MEMORY)
        self.option_vars = {}
        self.proc = None
        self.log_queue = queue.Queue()
        self.start_time = None
        self._build_ui()
        self.after(100, self._drain_log)
        self.after(500, self._refresh_game_state)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        header = ttk.Frame(self)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="BA 扫描工具箱", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.game_label = ttk.Label(header, text="游戏状态：检测中…", foreground="#888")
        self.game_label.pack(side="right")
        ttk.Button(header, text="刷新", width=6, command=self._refresh_game_state).pack(side="right", padx=6)

        cards = ttk.LabelFrame(self, text="选择扫描方法")
        cards.pack(fill="x", **pad)
        self.engine_frames = {}
        for engine in ENGINES:
            row = ttk.Frame(cards)
            row.pack(fill="x", padx=8, pady=4)
            radio = ttk.Radiobutton(
                row, value=engine.key, variable=self.engine_var,
                text=engine.title, command=self._on_engine_change)
            radio.pack(side="left", anchor="n")
            body = ttk.Frame(row)
            body.pack(side="left", fill="x", expand=True, padx=(4, 0))
            ttk.Label(body, text=engine.subtitle, foreground="#555", justify="left").pack(anchor="w")
            option_frame = ttk.Frame(body)
            option_frame.pack(fill="x")
            vars_for_engine = {}
            for label, key, default in engine.options:
                if key == "max":
                    ttk.Label(option_frame, text=label + ":").pack(side="left")
                    var = tk.StringVar(value=str(default))
                    ttk.Entry(option_frame, textvariable=var, width=8).pack(side="left", padx=4)
                else:
                    var = tk.BooleanVar(value=default)
                    ttk.Checkbutton(option_frame, text=label, variable=var).pack(side="left", padx=(0, 12))
                vars_for_engine[key] = var
            self.option_vars[engine.key] = vars_for_engine
            self.engine_frames[engine.key] = option_frame
            if engine.key != ENGINE_MEMORY:
                option_frame.pack_forget()

        self.option_note = ttk.Label(self, text="", foreground="#a60")
        self.option_note.pack(fill="x", padx=14, anchor="w")

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="开始扫描", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="打开输出文件夹", command=self._open_output, state="disabled")
        self.open_btn.pack(side="left", padx=6)
        self.elapsed_label = ttk.Label(actions, text="")
        self.elapsed_label.pack(side="right")

        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=14, wrap="none", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("warn", foreground="#b00")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, foreground="#666").pack(fill="x", padx=12, pady=(0, 6))

    def current_engine(self):
        for engine in ENGINES:
            if engine.key == self.engine_var.get():
                return engine
        return ENGINES[0]

    def _on_engine_change(self):
        for key, frame in self.engine_frames.items():
            if key == self.engine_var.get():
                frame.pack(fill="x")
            else:
                frame.pack_forget()
        engine = self.current_engine()
        notes = {
            ENGINE_MEMORY: "",
            ENGINE_INVENTORY: "提示：装备设计图需在游戏内切到「裝備」页后再跑一次，自动追加。",
            ENGINE_STUDENT: "提示：先停在学生详情页「基本情報」，从当前显示的学生开始按右方向键扫描。",
        }
        self.option_note.config(text=notes.get(engine.key, ""))

    def _refresh_game_state(self):
        running = game_running()
        self.game_label.config(
            text="游戏状态：运行中 ✓" if running else "游戏状态：未运行",
            foreground="#080" if running else "#b00")
        self.after(5000, self._refresh_game_state)

    def _collect_options(self, engine):
        values = {}
        for key, var in self.option_vars[engine.key].items():
            if key == "max":
                try:
                    values[key] = max(1, int(var.get()))
                except ValueError:
                    values[key] = 400
            elif isinstance(var, tk.BooleanVar):
                values[key] = var.get()
            else:
                values[key] = var.get()
        return values

    def _log(self, line, warn=False):
        self.log_queue.put((line, warn))

    def _drain_log(self):
        try:
            while True:
                line, warn = self.log_queue.get_nowait()
                if line == "__DONE__":
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.open_btn.config(state="normal")
                    self.status_var.config(text="就绪" if warn else "结束（有错误，见日志）")
                    self.start_time = None
                    continue
                self.log_text.config(state="normal")
                self.log_text.insert("end", line + "\n", ("warn",) if warn else ())
                if warn:
                    self.log_text.tag_add("warn", "end-2l", "end-1l")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        if self.proc and self.proc.poll() is None and self.start_time:
            minutes = (time.monotonic() - self.start_time) / 60
            self.elapsed_label.config(text=f"已运行 {minutes:.1f} 分钟")
        self.after(150, self._drain_log)

    def _start(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("提示", "已有任务在运行")
            return
        engine = self.current_engine()
        options = self._collect_options(engine)
        try:
            cmd, cwd = engine.build_cmd(options)
        except FileNotFoundError as error:
            messagebox.showerror("配置错误", str(error))
            return
        if engine.key in (ENGINE_INVENTORY, ENGINE_STUDENT) and not game_running():
            if not messagebox.askyesno("游戏未运行", "检测到游戏未运行，OCR 扫描需要游戏窗口。仍要继续吗？"):
                return
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._log(f"[启动] {engine.title}")
        self._log("[命令] " + " ".join(cmd))
        creation = subprocess.CREATE_NO_WINDOW if engine.key == ENGINE_MEMORY else 0
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=creation)
        except OSError as error:
            messagebox.showerror("启动失败", str(error))
            return
        self.start_time = time.monotonic()
        self.status_var.config(text=f"运行中：{engine.title}")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.open_dir = str(engine.open_dir_fn())
        threading.Thread(target=self._pump_output, args=(engine,), daemon=True).start()

    def _pump_output(self, engine):
        proc = self.proc
        assert proc and proc.stdout
        for raw in iter(proc.stdout.readline, b""):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    line = raw.decode("gbk")
                except UnicodeDecodeError:
                    line = raw.decode("utf-8", errors="replace")
            self._log(line.rstrip())
        code = proc.wait()
        self._log(f"[结束] 退出码 {code}", warn=code != 0)
        if code == 0:
            self._log(f"完成。输出目录：{self.open_dir}")
        self.log_queue.put(("__DONE__", code == 0))

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("确认停止", "强制结束扫描进程？（已扫部分大多已落盘，当前帧可能丢失）"):
                return
            try:
                self.proc.kill()
            except OSError:
                pass
            self._log("[手动停止]", warn=True)

    def _open_output(self):
        if getattr(self, "open_dir", None):
            open_path(self.open_dir)

def main():
    if "--selftest" in sys.argv:
        for engine in ENGINES:
            options = {"reconcile": False, "fresh": False, "once": False, "max": 400}
            try:
                cmd, cwd = engine.build_cmd(options)
                print(f"[OK] {engine.key}: {' '.join(cmd)}  (cwd={cwd})")
            except FileNotFoundError as error:
                print(f"[CONFIG-MISSING] {engine.key}: {error}")
        return 0
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
