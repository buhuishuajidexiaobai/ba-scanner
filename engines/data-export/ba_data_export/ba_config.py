"""共享配置：本机路径只存 config.json（不进仓库），缺省值见 config.example.json。"""
import json
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
EVIDENCE_DIR = PROJECT_DIR / "输出" / "evidence"
EXPORTS_DIR = PROJECT_DIR / "输出" / "exports"


def _load():
    for candidate in (PROJECT_DIR / "config.json", PACKAGE_DIR / "config.json"):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


CFG = _load()


def game_dir():
    value = CFG.get("game_dir")
    candidates = [Path(value)] if value else []
    candidates += [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\BlueArchive"),
        Path(r"D:\Steam\steamapps\common\BlueArchive"),
        Path(r"E:\Steam\steamapps\common\BlueArchive"),
        Path(r"C:\Steam\steamapps\common\BlueArchive"),
        Path(r"D:\GAMES\STEAM\steamapps\common\BlueArchive"),
    ]
    for candidate in candidates:
        if (candidate / "BlueArchive.exe").is_file():
            return candidate
    raise FileNotFoundError("未找到游戏目录：请在 config.json 设置 game_dir")


def dump_tool_dll():
    value = CFG.get("il2cpp_dump_tool_dll")
    if value and Path(value).is_file():
        return Path(value)
    for base in (PROJECT_DIR, PROJECT_DIR.parent):
        candidate = base / "third_party" / "Il2CppDumper-Static" / "Il2CppDumper.Static.dll"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("缺少 Il2CppDumper.Static.dll：使用发行包内置 third_party，或按 README 自行构建后配置路径")


def probe_dll():
    value = CFG.get("probe_dll")
    if value and Path(value).is_file():
        return Path(value)
    for base in (PROJECT_DIR, PROJECT_DIR.parent):
        candidate = base / "third_party" / "Il2CppDumper-Probe" / "Il2CppDumper.Probe.dll"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("缺少 Il2CppDumper.Probe.dll：使用发行包内置 third_party，或按 README 自行构建后配置路径")


def students_data():
    value = CFG.get("students_data")
    if value and Path(value).is_file():
        return Path(value)
    candidate = PROJECT_DIR / "data" / "students.min.json"
    return candidate if candidate.is_file() else None


def ocr_inventory_json():
    value = (CFG.get("reconciliation") or {}).get("ocr_inventory_json")
    if value and Path(value).is_file():
        return Path(value)
    return None


def liandu_glob_dir():
    value = (CFG.get("reconciliation") or {}).get("liandu_glob_dir")
    if value and Path(value).is_dir():
        return Path(value)
    return None
