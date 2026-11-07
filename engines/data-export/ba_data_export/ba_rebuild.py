"""游戏更新后的重新适配：离线重解析 → 探针验证注册地址 → 刷新布局与引用证据。不读取进程、不联网。

用法：python -I -B ba_data_export\\ba_rebuild.py
背景：上游 Il2CppDumper 自动识别在本客户端存在已知缺陷（genericMethodPointersCount 超阈值时
错误回移注册地址），本脚本按 CR+0x10 优先重试；探针内置计数一致性校验，错地址会快速失败。
"""
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def load(filename):
    spec = __import__("importlib").util.spec_from_file_location(filename.replace(".py", ""), Path(__file__).with_name(filename))
    module = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ba_config = load("ba_config.py")

ALL_FALSE_CONFIG = {
    "DumpMethod": False, "DumpField": False, "DumpProperty": False, "DumpAttribute": False,
    "DumpFieldOffset": False, "DumpMethodOffset": False, "DumpTypeDefIndex": False,
    "GenerateDummyDll": False, "GenerateStruct": False, "DummyDllAddToken": False,
    "RequireAnyKey": False, "ForceIl2CppVersion": False, "ForceVersion": 31,
    "ForceDump": False, "NoRedirectedPointer": False,
}
DB_REGEX = r"MX\.GameLogic\.DBModel\.(CharacterDB|ItemDB|EquipmentDB|WeaponDB|GearDB|ConsumableItemBaseDB)$"
REFS_INVENTORY_REGEX = r"refs:(CharacterInventoryObject|ItemInventoryObject|EquipmentInventoryObject|MX\.GameLogic\.DBModel\.(CharacterDB|ItemDB|EquipmentDB|WeaponDB|GearDB)$)"
REFS_ROOT_REGEX = r"refs:(^MXAccount$|^GameSession$)"
NATIVE_VERSIONS = ("29", "31", "27.2", "27.1", "27")
CR_SHIFTS = (0x10, 0x00, 0x08, -0x10, -0x08)


def prepare_adapter_build():
    adapter_dir = ba_config.dump_tool_dll().parent
    adapter_dir = adapter_dir.parent / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    source_dir = ba_config.dump_tool_dll().parent
    for source in source_dir.iterdir():
        if source.is_file() and source.suffix in (".dll", ".json"):
            shutil.copy2(source, adapter_dir / source.name)
    (adapter_dir / "config.json").write_text(json.dumps(ALL_FALSE_CONFIG), encoding="utf-8")
    return adapter_dir / "Il2CppDumper.Static.dll"


def read_buildid(game_dir):
    manifest = game_dir.parent.parent / "appmanifest_3557620.acf"
    if not manifest.is_file():
        return None
    match = re.search(r'"buildid"\s*"(\d+)"', manifest.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def run_dumper(adapter_dll, game_assembly, metadata, out_dir):
    out_dir.mkdir(parents=True, exist_ok=False)
    result = subprocess.run(
        ["dotnet", str(adapter_dll), str(game_assembly), str(metadata), str(out_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, cwd=str(Path(__file__).resolve().parent.parent))
    log = (result.stdout or "") + (result.stderr or "")
    (out_dir / "解析日志.txt").write_text(log, encoding="utf-8")
    code_candidates = list(dict.fromkeys(re.findall(r"CodeRegistration\s*:\s*([0-9a-fA-F]+)", log)))
    metadata_candidates = list(dict.fromkeys(re.findall(r"MetadataRegistration\s*:\s*([0-9a-fA-F]+)", log)))
    version_match = re.search(r"Metadata Version:\s*(\d+)", log)
    metadata_version = int(version_match.group(1)) if version_match else None
    return code_candidates, metadata_candidates, metadata_version, log


def try_probe(probe_dll, metadata, game_assembly, out_dir, filename, pattern, code_address, metadata_address, native_version):
    target = out_dir / filename
    result = subprocess.run(
        ["dotnet", str(probe_dll), str(metadata), str(target), pattern,
         str(game_assembly), native_version, hex(code_address), hex(metadata_address)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(Path(__file__).resolve().parent.parent))
    return result.returncode == 0 and target.is_file()


def find_working_addresses(probe_dll, metadata, game_assembly, out_dir, code_candidates, metadata_candidates):
    attempts = 0
    for native_version in NATIVE_VERSIONS:
        for shift in CR_SHIFTS:
            for code_text in code_candidates:
                code_address = (int(code_text, 16) + shift) & 0xFFFFFFFFFFFFFFFF
                for metadata_text in metadata_candidates:
                    metadata_address = int(metadata_text, 16)
                    attempts += 1
                    if try_probe(probe_dll, metadata, game_assembly, out_dir, "目标集合与实体.json", DB_REGEX, code_address, metadata_address, native_version):
                        return code_address, metadata_address, native_version, attempts
    return None, None, None, attempts


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    game_dir = ba_config.game_dir()
    game_assembly = game_dir / "GameAssembly.dll"
    metadata = game_dir / "BlueArchive_Data/il2cpp_data/Metadata/global-metadata.dat"
    if not game_assembly.is_file() or not metadata.is_file():
        raise SystemExit(f"游戏文件不存在：{game_dir}")
    probe_dll = ba_config.probe_dll()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = ba_config.PROJECT_DIR / "输出" / f"版本适配_{stamp}"
    buildid = read_buildid(game_dir)
    print(f"游戏 buildid：{buildid}；输出目录：{out_dir}")
    adapter_dll = prepare_adapter_build()
    print("离线解析中（自动识别有已知缺陷，失败也会提取候选地址）…")
    code_candidates, metadata_candidates, metadata_version, log = run_dumper(adapter_dll, game_assembly, metadata, out_dir / "解析")
    print(f"Metadata Version: {metadata_version}；候选 CodeRegistration={code_candidates}，MetadataRegistration={metadata_candidates}")
    if not code_candidates or not metadata_candidates:
        print("未找到候选注册地址（自动搜索全部失败）。请把 解析日志.txt 留档并提 issue。")
        raise SystemExit(1)
    if metadata_version is not None and metadata_version > 31:
        print(f"元数据版本 {metadata_version} 超出上游支持上限(31)，需等待 Il2CppDumper 更新后再适配。")
        raise SystemExit(1)
    print("探针验证候选地址中…")
    code_address, metadata_address, native_version, attempts = find_working_addresses(probe_dll, metadata, game_assembly, out_dir, code_candidates, metadata_candidates)
    if code_address is None:
        print(f"{attempts} 组候选全部未通过计数一致性校验。请把 解析日志.txt 留档并提 issue。")
        raise SystemExit(1)
    print(f"可用地址：CodeRegistration={hex(code_address)} MetadataRegistration={hex(metadata_address)} 原生布局={native_version}（尝试 {attempts} 次）")
    for filename, pattern in (("核心对象与物品基类.json", DB_REGEX), ("集合引用链.json", REFS_INVENTORY_REGEX), ("账号根引用.json", REFS_ROOT_REGEX)):
        if not try_probe(probe_dll, metadata, game_assembly, out_dir, filename, pattern, code_address, metadata_address, native_version):
            raise SystemExit(f"探针生成 {filename} 失败")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(Path(__file__).with_name("ba_layout_builder.py")),
         "--analysis-dir", str(out_dir), "--out", str(out_dir / "只读字段布局.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        cwd=str(Path(__file__).resolve().parent.parent))
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise SystemExit("布局生成失败")
    ba_config.EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / "只读字段布局.json", ba_config.EVIDENCE_DIR / "只读字段布局.json")
    for name in ("集合引用链.json", "账号根引用.json"):
        shutil.copy2(out_dir / name, ba_config.EVIDENCE_DIR / name)
    print(json.dumps({
        "buildid": buildid,
        "metadata_version": metadata_version,
        "native_version": native_version,
        "code_registration": hex(code_address),
        "metadata_registration": hex(metadata_address),
        "evidence_dir": str(out_dir),
        "updated": [str(ba_config.EVIDENCE_DIR / name) for name in ("只读字段布局.json", "集合引用链.json", "账号根引用.json")],
        "process_memory_read": False,
    }, ensure_ascii=False, indent=2))
    print("适配完成。下次导出：打开游戏进入大厅后运行 export_data.bat")


if __name__ == "__main__":
    main()
