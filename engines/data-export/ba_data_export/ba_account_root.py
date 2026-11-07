"""只读验证已知类型槽和账号单例，不扫描进程地址空间。

同时提供静态证据装载函数（source_types / slots_for_types / resolve_root），供导出流程复用。
"""
import argparse
import hashlib
import importlib.util
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

import pefile


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCESS = load("ba_process_reader.py")
ba_config = load("ba_config.py")


def source_types():
    evidence = ba_config.EVIDENCE_DIR
    return {entry["name"]: entry for name in ("集合引用链.json", "账号根引用.json") for entry in json.loads((evidence / name).read_text(encoding="utf-8"))["types"] if "<" not in entry["name"]}


def slots_for_types(binary, types):
    pe = pefile.PE(str(binary), fast_load=True)
    result = {}
    for name, entry in types.items():
        pattern = struct.pack("<Q", (1 << 29) | (entry["typeIndex"] << 1) | 1)
        matches = []
        for section in pe.sections:
            if section.Characteristics & 0x80000000 == 0:
                continue
            data = section.get_data()
            start = 0
            while True:
                index = data.find(pattern, start)
                if index < 0:
                    break
                if index % 8 == 0:
                    matches.append(section.VirtualAddress + index)
                start = index + 1
        result[name] = matches
    pe.close()
    return result


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resolve_root(process, module, types, slots):
    candidates = []
    for rva in slots["Nested"]:
        klass = process.u64(module["base"] + rva)
        if klass < 65536 or klass & 1:
            continue
        if process.class_name(klass) != "Nested":
            raise ValueError("单例类型名称不一致")
        declaring = process.u64(klass + 0x50)
        if process.class_name(declaring) != "MXAccount":
            raise ValueError("Nested 不是 MXAccount 的嵌套类")
        fields = process.fields(klass, 1)
        if fields[0]["name"] != "instance" or fields[0]["offset"] != 0:
            raise ValueError("单例静态字段不一致")
        static_fields = process.u64(klass + 0xB8)
        account = process.u64(static_fields)
        if not account:
            continue
        account_class = process.u64(account)
        if process.class_name(account_class) != "MXAccount":
            raise ValueError("当前账号对象类名不一致")
        candidates.append((account, account_class))
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        raise ValueError(f"无法确定唯一账号根（候选数量 {len(candidates)}）——请确认已登录并进入大厅")
    return candidates[0]


def validate_inventory(process, account, kind, offset, expected_name, types):
    pointer = process.u64(account + offset)
    klass = process.u64(pointer)
    if process.class_name(klass) != expected_name:
        raise ValueError("库存对象身份不匹配")
    own = process.fields(klass, len(types[expected_name]["fields"]))
    expected = {field["name"]: field["offset"] for field in types[expected_name]["fields"]}
    if {field["name"]: field["offset"] for field in own} != expected:
        raise ValueError("库存自身字段布局与文件证据不一致")
    parent = process.u64(klass + 0x58)
    if process.class_name(parent) != "InventoryObjectBase`1":
        raise ValueError("库存父类不匹配")
    parent_fields = process.fields(parent, 4)
    asset = [field for field in parent_fields if field["name"] == "assetTable"]
    if len(asset) != 1 or not 16 <= asset[0]["offset"] <= 128:
        raise ValueError("泛型库存字典字段无法确认")
    dictionary = process.u64(pointer + asset[0]["offset"])
    dictionary_class = process.u64(dictionary)
    if process.class_name(dictionary_class) != "System.Collections.Generic.Dictionary`2":
        raise ValueError("库存容器并非字典")
    return {"kind": kind, "object": pointer, "dictionary": dictionary, "dictionary_class": dictionary_class, "class_name": expected_name, "asset_table_offset": asset[0]["offset"], "runtime_fields": own, "parent_fields": parent_fields}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise ValueError("需要全新输出文件")
    profile = json.loads((ba_config.EVIDENCE_DIR / "只读字段布局.json").read_text(encoding="utf-8"))
    game_dir = ba_config.game_dir()
    binary = game_dir / "GameAssembly.dll"
    if hash_file(binary) != profile["gameassembly_sha256"]:
        raise ValueError("磁盘游戏版本改变，停止")
    types = source_types()
    slots = slots_for_types(binary, {"Nested": types["Nested"]})
    report = {"time_utc": datetime.now(timezone.utc).isoformat(), "stage": "runtime_root_validation", "process_id": args.pid, "slot_counts": {name: len(values) for name, values in slots.items()}, "status": "not_started", "account_values_exported": False}
    process = None
    try:
        with ACCESS.ReadOnlyProcess(args.pid) as process:
            if Path(process.image_path()).resolve() != (game_dir / "BlueArchive.exe").resolve():
                raise ValueError("进程映像路径不符")
            module = process.module("GameAssembly.dll")
            if Path(module["path"]).resolve() != binary.resolve():
                raise ValueError("模块路径不符")
            account, account_class = resolve_root(process, module, types, slots)
            account_fields = process.fields(account_class, len(types["MXAccount"]["fields"]))
            offsets = {field["name"]: field["offset"] for field in account_fields}
            checks = []
            for kind, field, name in (("character", "CharacterInventory", "CharacterInventoryObject"), ("equipment", "EquipmentInventory", "EquipmentInventoryObject"), ("item", "ItemInventory", "ItemInventoryObject")):
                expected = next(f["offset"] for f in types["MXAccount"]["fields"] if f["name"] == field)
                if offsets[field] != expected:
                    raise ValueError("账号库存字段布局不一致")
                check = validate_inventory(process, account, kind, expected, name, types)
                checks.append({key: value for key, value in check.items() if key not in ("object", "dictionary", "dictionary_class")})
            report.update(status="root_and_inventory_types_verified", inventories=checks)
    except Exception as exc:
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc))
    finally:
        if process:
            report.update(bytes_read=process.total_bytes, read_calls=process.read_count, handle_closed=process.handle is None)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "stopped":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
