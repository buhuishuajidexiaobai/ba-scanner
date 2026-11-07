# -*- coding: utf-8 -*-
"""
BA 学生练度扫描器 (Blue Archive 学生详情页自动扫描)

流程: 在学生详情页(基本情報页签)从第一个学生开始, 自动按键盘右方向键切换学生,
逐屏截图 + RapidOCR 离线识别练度, 直到翻回第一个学生(指纹: 名字+绊+等级)为止。
扫到列表尾部若不循环, 连续 3 次画面无变化也会自动收工。

用法:
  python ba_student_scan.py            # 正式扫描 (从当前学生开始, 覆盖全列表)
  python ba_student_scan.py --once     # 单帧调试: 不按键, 识别当前画面并打印解析结果
  python ba_student_scan.py --max 500  # 覆盖学生数上限
  python ba_student_scan.py --debug-frames   # 每个学生另存截图到 输出/debug

复用: RapidOCR + mss + opencv, 窗口/截图/引擎模式与姊妹项目
ba-inventory-ocr (项目/BA库存抓包) 的 ba_queue_collector.py 同源。
"""

import argparse
import ctypes
import datetime
import json
import os
import re
import subprocess
import sys
import time

import mss

import cv2
import numpy as np
import onnxruntime as ort
import rapidocr_onnxruntime.utils as r_utils
from rapidocr_onnxruntime import RapidOCR
from zhconv import convert as zh_convert

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "输出")
DEBUG_DIR = os.path.join(OUT_DIR, "debug")

# ---------------- OCR 引擎 (DirectML GPU 加速, 失败回退 CPU) ----------------

HAS_DML = "DmlExecutionProvider" in ort.get_available_providers()

def make_engine():
    sess_opt = ort.SessionOptions()
    sess_opt.log_severity_level = 4
    sess_opt.enable_cpu_mem_arena = False
    sess_opt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    ep_list = []
    if HAS_DML:
        ep_list.append(("DmlExecutionProvider", {"device_id": 0}))
    ep_list.append(("CPUExecutionProvider", {}))

    _orig_init = r_utils.OrtInferSession.__init__
    def _custom_init(self, config):
        self._verify_model(config["model_path"])
        self.session = ort.InferenceSession(config["model_path"], sess_options=sess_opt, providers=ep_list)
    r_utils.OrtInferSession.__init__ = _custom_init

    eng = RapidOCR()
    r_utils.OrtInferSession.__init__ = _orig_init
    return eng

ENGINE = None

# ---------------- 窗口 / 截屏 / 按键 ----------------

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()   # 与 mss 的 DPI 感知对齐: 坐标统一为物理像素
SCT = mss.MSS()
WIN = (0, 0, 100, 100)        # 游戏窗口物理像素矩形 (left, top, right, bottom)

VK_RIGHT = 0x27
SC_RIGHT = 0x4D
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

def send_right():
    user32.keybd_event(VK_RIGHT, SC_RIGHT, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(0.06)
    user32.keybd_event(VK_RIGHT, SC_RIGHT, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

def window_rect(hwnd):
    """游戏窗口矩形(物理像素); 取不到时回退主屏"""
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
    rc = RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rc)) \
            and rc.right > rc.left and rc.bottom > rc.top:
        return (rc.left, rc.top, rc.right, rc.bottom)
    m = SCT.monitors[1]
    return (m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])

def activate_game():
    """找到 BlueArchive 窗口 → 还原(若最小化) → 置前。返回 hwnd 或 None"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-Process BlueArchive -ErrorAction SilentlyContinue | "
         "Where-Object { $_.MainWindowHandle -ne 0 } | "
         "Select-Object -First 1).MainWindowHandle"],
        capture_output=True, text=True, timeout=30).stdout.strip()
    if not out.isdigit():
        print("未找到 BlueArchive 窗口: 请先启动游戏并停在学生详情页", flush=True)
        return None
    hwnd = int(out)
    user32.ShowWindow(hwnd, 9)            # SW_RESTORE: 最小化则还原
    user32.SetForegroundWindow(hwnd)
    time.sleep(1.2)
    ok = user32.GetForegroundWindow() == hwnd
    print(f"game window hwnd={hwnd} foreground={'OK' if ok else 'FAILED(请手动点一下游戏窗口)'}",
          flush=True)
    time.sleep(0.8)
    return hwnd

def grab_bgr():
    m = {"left": WIN[0], "top": WIN[1],
         "width": WIN[2] - WIN[0], "height": WIN[3] - WIN[1]}
    raw = np.asarray(SCT.grab(m), dtype=np.uint8)
    return np.ascontiguousarray(raw[:, :, :3])

def ocr_lines(arr):
    """RapidOCR 全屏识别 → [{cx, cy, text}] (中心坐标, 相对窗口)"""
    res, _ = ENGINE(arr)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                box, text = it[0], it[1].strip()
                if not text:
                    continue
                xs = [q[0] for q in box]
                ys = [q[1] for q in box]
                out.append({"cx": float(np.mean(xs)), "cy": float(np.mean(ys)), "text": text})
    return out

def local_ocr(img, x0, x1, y0, y1, scale=4):
    """局部区域裁剪 → 放大 → 二次 OCR (抢救全屏漏检的小字), 坐标映射回全窗口"""
    H, W = img.shape[:2]
    roi = img[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]
    if roi.size == 0:
        return []
    big = cv2.resize(roi, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    res, _ = ENGINE(big)
    out = []
    if res:
        for it in res:
            if isinstance(it, (list, tuple)) and len(it) >= 3:
                box, text = it[0], it[1].strip()
                if not text:
                    continue
                xs = [q[0] for q in box]
                ys = [q[1] for q in box]
                out.append({"cx": x0 * W + float(np.mean(xs)) / scale,
                            "cy": y0 * H + float(np.mean(ys)) / scale, "text": text})
    return out

# ---------------- 画面变化检测 (左下名字带) ----------------

def name_band_hash(arr, W, H):
    """左下名字带灰度图 (切学生必然变化)"""
    x0, x1 = int(0.03 * W), int(0.36 * W)
    y0, y1 = int(0.79 * H), int(0.90 * H)
    band = arr[y0:y1, x0:x1]
    return cv2.resize(cv2.cvtColor(band, cv2.COLOR_BGR2GRAY), (64, 16))

def wait_switched(prev_hash, timeout=6.0):
    """按右键后等待画面切换并稳定。返回 True=已切换, False=超时无变化"""
    t0 = time.time()
    changed = False
    last = prev_hash
    stable = 0
    while time.time() - t0 < timeout:
        time.sleep(0.30)
        cur = name_band_hash(grab_bgr(), WIN[2] - WIN[0], WIN[3] - WIN[1])
        diff = float(np.mean(np.abs(cur.astype(int) - last.astype(int))))
        if diff > 3.0:
            changed = True
            stable = 0
        else:
            stable += 1
        last = cur
        if changed and stable >= 3:
            time.sleep(0.25)
            return True
        if not changed and time.time() - t0 > timeout:
            break
    return changed

# ---------------- 练度解析 ----------------

NUM_RE = re.compile(r"^[0-9,]+$")
LV_RE = re.compile(r"(?i)l[v1ily][.,]?\s*(\d{1,3})")   # 宽容 Lv/LY/LV/Ly (OCR 常把 v 认成 y)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

def _clean(t):
    return t.replace(" ", "").replace(" ", "")

def _num(t):
    """'58,117' → 58117"""
    return int(t.replace(",", ""))

def count_stars(img, W, H, x0, x1, y0, y1, hue_lo, hue_hi, smin=120, vmin=130, erode=0):
    """在相对坐标区域用 HSV 颜色连通域数星星 (学生=金黄, 武器=蓝); erode 用于分开粘连的星"""
    if img is None:
        return None
    sx0, sx1 = int(x0 * W), int(x1 * W)
    sy0, sy1 = int(y0 * H), int(y1 * H)
    roi = img[sy0:sy1, sx0:sx1]
    if roi.size == 0:
        return None
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (hue_lo, smin, vmin), (hue_hi, 255, 255))
    if erode:
        mask = cv2.erode(mask, np.ones((erode, erode), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    min_a = max(8, int(0.00004 * W * H))   # 面积下限随分辨率缩放
    cnt = 0
    for i in range(1, n):
        a = stats[i, cv2.CC_STAT_AREA]
        if min_a <= a <= 0.004 * W * H:
            cnt += 1
    return cnt if cnt else None

def parse_profile(lines, img=None):
    """OCR 行 → 练度 dict。相对坐标以 2560x1600 实测 UI 校准(等比缩放通用)。"""
    W, H = float(WIN[2] - WIN[0]), float(WIN[3] - WIN[1])
    p = {"name": None, "bond": None, "level": None, "stars": None, "role": None,
         "hp": None, "atk": None, "dfn": None, "heal": None, "lib": None,
         "skills": {"ex": None, "base": None, "enh": None, "sub": None},
         "weapon": {"type": None, "level": None, "stars": None},
         "gear": [None, None, None, None]}

    for it in lines:
        it["t"] = _clean(it["text"])
        it["s"] = zh_convert(it["t"], "zh-hans")   # 简体副本: OCR 繁简漂移时标签仍可匹配

    # ---- 左下: 绊 / 名字 / 等级 / 角色 / 星 ----
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        t = it["t"].replace("★", "")
        if 0.79 <= r_y <= 0.86 and r_x < 0.11 and t.isdigit() and 1 <= int(t) <= 100:
            p["bond"] = int(t)                        # 绊 (❤图标右侧数字)
    if p["bond"] is None and img is not None:         # 绊小字全屏常漏检 → 局部放大重识别
        for it in local_ocr(img, 0.01, 0.09, 0.785, 0.855, scale=5):
            t = _clean(it["text"])
            if t.isdigit() and 1 <= int(t) <= 100:
                p["bond"] = int(t)
                break
    name_it = None
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        t = re.sub(r"^[0-9]+", "", it["t"].replace("★", ""))   # 剥离误粘连的绊数字前缀
        if 0.80 <= r_y <= 0.858 and 0.04 <= r_x <= 0.27 and CJK_RE.search(t):
            if name_it is None or len(t) > len(name_it["t"]):
                name_it = it                          # 名字 (含 CJK 的最长行)
    if name_it:
        p["name"] = name_it["t"]
    elif img is not None:                             # 名字随机漏检 → 局部放大重识别
        cands = []
        for it in local_ocr(img, 0.03, 0.28, 0.795, 0.865, scale=3):
            t = re.sub(r"^[0-9]+", "", _clean(it["text"]).replace("★", ""))
            if CJK_RE.search(t):
                cands.append(t)
        if cands:
            p["name"] = max(cands, key=len)
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        m = LV_RE.match(it["t"])
        if m and 0.838 <= r_y <= 0.90 and r_x < 0.30:
            p["level"] = int(m.group(1))              # 学生等级 (Lv.90 黄字)
            break
    if p["level"] is None and img is not None:        # 等级随机漏检 → 局部放大重识别
        for it in local_ocr(img, 0.02, 0.16, 0.84, 0.90, scale=4):
            m = LV_RE.match(_clean(it["text"]))
            if m:
                p["level"] = int(m.group(1))
                break
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        if 0.80 <= r_y <= 0.86 and r_x >= 0.25 and it["t"].upper() in ("STRIKER", "SPECIAL"):
            p["role"] = it["t"].upper()               # 定位 (STRIKER/SPECIAL)
            break
    p["stars"] = count_stars(img, W, H, 0.19, 0.30, 0.80, 0.845, 20, 33, smin=150, vmin=180)

    # ---- 右面板: 四维(结构化行列带, 兼容 label+数字合并 token) + 解放等级 ----
    def grab_stat(ry0, ry1, rx0, rx1):
        cands = []
        for it in lines:
            r_x, r_y = it["cx"] / W, it["cy"] / H
            if not (ry0 <= r_y <= ry1 and rx0 <= r_x <= rx1):
                continue
            if LV_RE.match(it["t"]):                  # 排除 Lv.25 徽章
                continue
            if NUM_RE.match(it["t"]):
                cands.append((it["cx"], _num(it["t"])))
            else:
                m = re.search(r"\d[\d,]{1,}", it["t"])
                if m:
                    cands.append((it["cx"], _num(m.group())))
        return min(cands, key=lambda v: v[0])[1] if cands else None
    p["hp"] = grab_stat(0.345, 0.368, 0.575, 0.660)   # 最大體力 58117
    p["atk"] = grab_stat(0.345, 0.368, 0.715, 0.800)  # 攻擊力 9068
    p["dfn"] = grab_stat(0.385, 0.410, 0.545, 0.660)  # 防禦力 377
    p["heal"] = grab_stat(0.385, 0.410, 0.690, 0.800) # 治癒力 5763
    for it in lines:                                  # 解放等级: 体力行右侧 Lv.25 徽章
        r_x, r_y = it["cx"] / W, it["cy"] / H
        m = LV_RE.match(it["t"])
        if m and 0.345 <= r_y <= 0.368 and 0.615 <= r_x <= 0.780:
            p["lib"] = int(m.group(1))
            break
    if p["lib"] is None and img is not None:          # 黄底小徽章全屏常漏 → 局部放大重识别
        for it in local_ocr(img, 0.615, 0.78, 0.335, 0.375, scale=4):
            m = LV_RE.match(_clean(it["text"]))
            if m:
                p["lib"] = int(m.group(1))
                break

    # ---- 技能四卡: 标题锚点(简体匹配) → 主行找值, 主行漏检时局部放大 OCR 兜底 ----
    skill_titles = [("EX技能", "ex", 0.580), ("基本技能", "base", 0.662),
                    ("强化技能", "enh", 0.746), ("子技能", "sub", 0.828)]
    used = set()
    for label, key, default_x in skill_titles:
        anc_x = default_x
        for it in lines:
            if id(it) in used:
                continue
            if label in it["s"] and 0.42 <= it["cy"] / H <= 0.52:
                used.add(id(it))
                anc_x = it["cx"] / W
                break
        best = None
        for it in lines:
            if id(it) in used:
                continue
            m = LV_RE.fullmatch(it["t"])
            is_max = it["t"].upper() == "MAX"
            if not m and not is_max:
                continue
            dx = abs(it["cx"] / W - anc_x)
            r_y = it["cy"] / H
            if dx < 0.045 and 0.50 <= r_y <= 0.60:
                if best is None or r_y < best[0]:
                    best = (r_y, it, m)
        if best:
            used.add(id(best[1]))
            p["skills"][key] = "MAX" if best[2] is None else int(best[2].group(1))
        elif img is not None:                         # 基本技能 MAX 全屏常漏检 → 局部放大
            for it in local_ocr(img, anc_x - 0.045, anc_x + 0.045, 0.50, 0.605, scale=3):
                t = _clean(it["text"])
                if t.upper() == "MAX":
                    p["skills"][key] = "MAX"
                    break
                m = LV_RE.fullmatch(t)
                if m:
                    p["skills"][key] = int(m.group(1))
                    break

    # ---- 武器 (类型 + 等级 + 星) ----
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        m = LV_RE.match(it["t"])
        if m and 0.60 <= r_y <= 0.715 and 0.56 <= r_x <= 0.72:
            p["weapon"]["level"] = int(m.group(1))    # 武器 Lv.50 (OCR 常出 Ly.50)
            break
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        if 0.62 <= r_y <= 0.73 and 0.48 <= r_x <= 0.60 and re.fullmatch(r"[A-Z]{2}", it["t"]):
            p["weapon"]["type"] = it["t"]             # MG / AR / ...
            break
    p["weapon"]["stars"] = count_stars(img, W, H, 0.75, 0.88, 0.68, 0.735, 90, 115,
                                       smin=100, vmin=220, erode=3)

    # ---- 装备四槽 (Lv.N / T角标 / EMPTY 按 x 聚槽) ----
    items = []
    for it in lines:
        r_x, r_y = it["cx"] / W, it["cy"] / H
        if not (0.72 <= r_y <= 0.86 and 0.50 <= r_x <= 0.92):
            continue
        t = it["t"].upper()
        if LV_RE.fullmatch(t) or re.fullmatch(r"T\d{1,2}", t) or t == "EMPTY":
            items.append((it["cx"], it["cy"], t))
        elif re.fullmatch(r"\d{1,2}", t) and 0.815 <= r_y <= 0.85:
            # T 角标被 OCR 吞掉 "T" 的纠错: "12"→T2, "11"→T1, "10"→T10, "9"→T9
            tier = "T" + (t[1:] if len(t) == 2 and t[0] == "1" and t != "10" else t)
            items.append((it["cx"], it["cy"], tier))
    items.sort(key=lambda v: v[0])
    slots, cur = [], []
    for cx, cy, t in items:
        if cur and cx - cur[-1][0] > 0.045 * W:
            slots.append(cur)
            cur = []
        cur.append((cx, cy, t))
    if cur:
        slots.append(cur)
    for i, slot in enumerate(slots[:4]):
        lv, tier, empty = None, None, False
        for _, _, t in slot:
            m = LV_RE.fullmatch(t)
            if m:
                lv = int(m.group(1))
            elif re.fullmatch(r"T\d{1,2}", t):
                tier = t
            elif t == "EMPTY":
                empty = True
        p["gear"][i] = None if empty else {"tier": tier, "level": lv}
    if img is not None and any(g and g.get("tier") is None for g in p["gear"] if g):
        # T 角标小字全屏常漏 → tier 行整行局部放大补读, 按 x 归入最近槽
        slot_x = [0.565 + 0.09 * j for j in range(4)]
        for it in local_ocr(img, 0.50, 0.92, 0.815, 0.848, scale=5):
            t = _clean(it["text"]).upper()
            if re.fullmatch(r"\d{1,2}", t):
                t = "T" + (t[1:] if len(t) == 2 and t[0] == "1" and t != "10" else t)
            if not re.fullmatch(r"T\d{1,2}", t):
                continue
            j = min(range(4), key=lambda k: abs(it["cx"] / W - slot_x[k]))
            if p["gear"][j] and p["gear"][j]["tier"] is None:
                p["gear"][j]["tier"] = t
    return p

# ---------------- 输出 ----------------

def fmt_val(v):
    return "—" if v is None else str(v)

def render_txt(rows, meta):
    L = []
    L.append("《蔚蓝档案》学生练度扫描")
    L.append(f"时间: {meta['time']}    窗口: {meta['win']}    学生数: {len(rows)}")
    L.append("=" * 72)
    for i, (r, p, *_) in enumerate(rows, 1):
        stars = "★" + str(p["stars"]) if p["stars"] is not None else "★?"
        wstars = "★" + str(p["weapon"]["stars"]) if p["weapon"]["stars"] is not None else ""
        sk = p["skills"]
        gear_txt = " / ".join(
            "空" if g is None else f"{fmt_val(g['tier'])}·Lv.{fmt_val(g['level'])}"
            for g in p["gear"])
        L.append("")
        L.append(f"[{i:>3}] {fmt_val(p['name'])}   Lv.{fmt_val(p['level'])}   {stars}"
                 f"   绊{fmt_val(p['bond'])}   {fmt_val(p['role'])}")
        L.append(f"      体力 {fmt_val(p['hp'])} · 攻击 {fmt_val(p['atk'])} · "
                 f"防御 {fmt_val(p['dfn'])} · 治愈 {fmt_val(p['heal'])} · 解放Lv.{fmt_val(p['lib'])}")
        L.append(f"      技能 EX:{fmt_val(sk['ex'])} / 基本:{fmt_val(sk['base'])}"
                 f" / 强化:{fmt_val(sk['enh'])} / 子:{fmt_val(sk['sub'])}")
        L.append(f"      武器 {fmt_val(p['weapon']['type'])} Lv.{fmt_val(p['weapon']['level'])} {wstars}")
        L.append(f"      装备 {gear_txt}")
    L.append("")
    L.append("=" * 72)
    L.append("(★? = 星数识别失败; 数值缺失为 — ; 建议附上 输出/debug 截图人工核对)")
    return "\n".join(L)

def save_raw_json(rows, meta):
    path = os.path.join(OUT_DIR, f"练度扫描_{meta['stamp']}_raw.json")
    slim = []
    for _, p, *rest in rows:
        item = dict(p)
        if rest:                                   # 附带本屏 OCR 原始行, 便于排障
            item["_ocr_lines"] = [{"cx": round(it["cx"]), "cy": round(it["cy"]),
                                   "text": it["text"]} for it in rest[0]]
        slim.append(item)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": slim}, f, ensure_ascii=False, indent=1)
    return path

class Tee:
    def __init__(self, fp):
        self.fp, self.stdout = fp, sys.stdout
    def write(self, s):
        self.fp.write(s)
        self.stdout.write(s)
    def flush(self):
        self.fp.flush()
        self.stdout.flush()

# ---------------- 主流程 ----------------

def fingerprint(p):
    return (p["name"], p["bond"], p["level"])

def scan_one(img):
    lines = ocr_lines(img)
    p = parse_profile(lines, img)
    return p, lines

def main():
    global ENGINE, WIN
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单帧调试: 不按键, 只识别当前画面")
    ap.add_argument("--max", type=int, default=400, help="学生数上限(防呆)")
    ap.add_argument("--miss-limit", type=int, default=3, help="按右键无反应的容忍次数(列表尾部不循环时收工)")
    ap.add_argument("--debug-frames", action="store_true", help="每个学生另存截图到 输出/debug")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    print("[引擎] 正在加载 RapidOCR ...", flush=True)
    ENGINE = make_engine()
    print(f"[引擎] 就绪 ({'DirectML GPU' if HAS_DML else 'CPU'})", flush=True)

    hwnd = activate_game()
    if not hwnd:
        sys.exit(1)
    WIN = window_rect(hwnd)
    W, H = WIN[2] - WIN[0], WIN[3] - WIN[1]
    print(f"[窗口] {W}x{H} @ ({WIN[0]},{WIN[1]})", flush=True)

    img = grab_bgr()
    p0, lines0 = scan_one(img)
    if not p0["name"] and p0["level"] is None and p0["bond"] is None:
        print("[中止] 当前画面未识别到学生练度信息。请确认游戏停在学生详情页(基本情報)后重试;", flush=True)
        print("       若页面正确, 请跑 --once 把输出发给维护者校准解析规则。", flush=True)
        os.makedirs(DEBUG_DIR, exist_ok=True)
        cv2.imencode(".png", img)[1].tofile(os.path.join(DEBUG_DIR, "未识别画面.png"))
        sys.exit(2)

    if args.once:
        stamp = datetime.datetime.now().strftime("%H%M%S")
        os.makedirs(DEBUG_DIR, exist_ok=True)
        cv2.imencode(".png", img)[1].tofile(os.path.join(DEBUG_DIR, f"单帧_{stamp}.png"))
        print(f"\n[解析结果] 窗口 {W}x{H}")
        print(json.dumps(p0, ensure_ascii=False, indent=1))
        print("\n[OCR 全部行] cx | cy | text")
        for it in lines0:
            print(f"  {it['cx']:7.0f} | {it['cy']:7.0f} | {it['text']}")
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(OUT_DIR, f"练度扫描_{stamp}_日志.txt")
    fp_log = open(log_path, "w", encoding="utf-8")
    sys.stdout = Tee(fp_log)
    print(f"[开始] {datetime.datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)

    rows = [(fingerprint(p0), p0, lines0)]
    fp0 = fingerprint(p0)
    print(f"[  1] {p0['name']} Lv.{p0['level']} 绊{p0['bond']}", flush=True)
    if args.debug_frames:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        cv2.imencode(".png", img)[1].tofile(os.path.join(DEBUG_DIR, f"{stamp}_0001_{p0['name']}.png"))

    miss = 0
    try:
        for idx in range(2, args.max + 1):
            prev = name_band_hash(grab_bgr(), W, H)
            send_right()
            moved = wait_switched(prev)
            if not moved:
                miss += 1
                print(f"[警告] 按右键无画面变化 ({miss}/{args.miss_limit})", flush=True)
                if miss >= args.miss_limit:
                    print("[结束] 列表尾部(不循环)或失去焦点, 收工。", flush=True)
                    break
                continue
            miss = 0
            img = grab_bgr()
            p, ln = scan_one(img)
            fp = fingerprint(p)
            if len(rows) >= 2 and fp == fp0:
                print(f"[结束] 已回到第一个学生 {fp0[0]}, 完整循环扫描完毕。", flush=True)
                break
            rows.append((fp, p, ln))
            print(f"[{idx:>3}] {fmt_val(p['name'])} Lv.{fmt_val(p['level'])} 绊{fmt_val(p['bond'])}", flush=True)
            if args.debug_frames:
                cv2.imencode(".png", img)[1].tofile(
                    os.path.join(DEBUG_DIR, f"{stamp}_{idx:04d}_{p['name'] or '?'}.png"))
        else:
            print(f"[结束] 达到学生数上限 {args.max}, 收工。", flush=True)
    except KeyboardInterrupt:
        print("\n[中断] Ctrl+C, 保存已扫描数据。", flush=True)

    meta = {"time": f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}", "win": f"{W}x{H}",
            "stamp": stamp}
    txt = render_txt(rows, meta)
    txt_path = os.path.join(OUT_DIR, f"练度扫描_{stamp}.txt")
    with open(txt_path, "w", encoding="utf-8-sig") as f:
        f.write(txt)
    json_path = save_raw_json(rows, meta)
    print(f"\n[输出] {txt_path}", flush=True)
    print(f"[输出] {json_path}", flush=True)
    print(f"[完成] 共 {len(rows)} 名学生", flush=True)
    fp_log.close()
    sys.stdout = sys.__stdout__

if __name__ == "__main__":
    main()
