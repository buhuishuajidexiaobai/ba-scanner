"""有界只读样本：少量记录 + 双读一致性校验，不保存原始内存，不做全量导出。"""
import argparse
import importlib.util
import json
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCESS = load("ba_process_reader.py")
LAYOUT = load("ba_layout_builder.py")
ROOTCHECK = load("ba_account_root.py")
ba_config = load("ba_config.py")

TYPES = ROOTCHECK.source_types()
INVENTORY_SPECS = [
    {"kind": "item", "account_field": "ItemInventory", "inventory_type": "ItemInventoryObject", "wrapper_type": "ItemObject", "db_type": "MX.GameLogic.DBModel.ItemDB"},
    {"kind": "equipment", "account_field": "EquipmentInventory", "inventory_type": "EquipmentInventoryObject", "wrapper_type": "EquipmentObject", "db_type": "MX.GameLogic.DBModel.EquipmentDB"},
    {"kind": "character", "account_field": "CharacterInventory", "inventory_type": "CharacterInventoryObject", "wrapper_type": "CharacterObject", "db_type": "MX.GameLogic.DBModel.CharacterDB"},
]
SAMPLE_LIMIT = 3
WALK_LIMIT = 200


def offset_of(type_name, field_name):
    return next(field["offset"] for field in TYPES[type_name]["fields"] if field["name"] == field_name)


def simple_name(full_name):
    return full_name.rsplit(".", 1)[-1]


def find_pid():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "Get-Process -Name BlueArchive -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
        capture_output=True, text=True, timeout=20)
    pids = sorted({int(token) for token in result.stdout.split() if token.isdigit()})
    if len(pids) != 1:
        raise ValueError(f"需要恰好一个 BlueArchive 进程，实际找到 {len(pids)} 个——请先启动游戏并登录进入大厅")
    return pids[0]


def verify_class(process, address, expected_name, what):
    klass = process.u64(address)
    actual = process.class_name(klass)
    if actual != expected_name:
        raise ValueError(f"{what} 类型不符：期望 {expected_name}，实际 {actual}")
    return klass


def read_record(process, wrapper, spec, profile):
    verify_class(process, wrapper, spec["wrapper_type"], "样本对象")
    layout = profile["layouts"][spec["kind"]]
    db = process.u64(wrapper + offset_of(spec["wrapper_type"], "<DB>k__BackingField"))
    verify_class(process, db, spec["db_type"], "DB 记录")
    data = process.read(db, layout["minimum_bytes"])
    record = LAYOUT.decode_record(layout, data)
    record["record_kind"] = spec["kind"]
    return record, db


def sample_equipment_ids(process, db):
    list_address = process.u64(db + offset_of("MX.GameLogic.DBModel.CharacterDB", "<EquipmentServerIds>k__BackingField"))
    if not list_address:
        return {"equipment_list_size": None, "equipment_server_ids_sample": []}
    list_klass = verify_class(process, list_address, "System.Collections.Generic.List`1", "EquipmentServerIds")
    offsets = {field["name"]: field["offset"] for field in process.fields(list_klass, process.field_count(list_klass))}
    size = process.i32(list_address + offsets["_size"])
    if not 0 <= size <= 1_000_000:
        raise ValueError("装备关联列表长度超出范围")
    items = process.u64(list_address + offsets["_items"])
    info = process.array_info(items)
    if info["element_name"] and simple_name(info["element_name"]) != "Int64":
        raise ValueError("装备关联列表元素类型不符")
    sample = [process.u64(info["data"] + 8 * index) for index in range(min(3, size))]
    return {"equipment_list_size": size, "equipment_server_ids_sample": sample}


def walk_once(process, account, spec, profile):
    pointer = process.u64(account + offset_of("MXAccount", spec["account_field"]))
    verify_class(process, pointer, spec["inventory_type"], "库存对象")
    dictionary = process.u64(pointer + 24)
    verify_class(process, dictionary, "System.Collections.Generic.Dictionary`2", "库存容器")
    dict_klass = process.u64(dictionary)
    offsets = {field["name"]: field["offset"] for field in process.fields(dict_klass, process.field_count(dict_klass))}
    required = ("_buckets", "_entries", "_count", "_freeCount", "_version")
    seen = set()
    for name in required:
        offset = offsets.get(name)
        if offset is None or not 16 <= offset <= 256 or offset in seen:
            raise ValueError(f"字典字段 {name} 偏移异常")
        seen.add(offset)
    buckets = process.u64(dictionary + offsets["_buckets"])
    entries = process.u64(dictionary + offsets["_entries"])
    count = process.i32(dictionary + offsets["_count"])
    free_count = process.i32(dictionary + offsets["_freeCount"])
    version_first = process.i32(dictionary + offsets["_version"])
    if not (0 <= count <= 10_000_000 and 0 <= free_count <= count) or not buckets or not entries:
        raise ValueError("库存字典计数或数组指针异常")
    bucket_info = process.array_info(buckets)
    if simple_name(bucket_info["element_name"]) != "Int32" or not 0 <= bucket_info["max_length"] <= 10_000_000 or count > bucket_info["max_length"]:
        raise ValueError("字典桶数组校验失败")
    entry_info = process.array_info(entries)
    if not simple_name(entry_info["element_name"]).endswith("Entry") or count > entry_info["max_length"]:
        raise ValueError("字典条目数组校验失败")
    # 本运行时 FieldInfo 报告的 Entry 偏移与数组实际存放不一致；
    # 实测：数组 element_size(0x100) 为真实步长，条目为经典布局 hash@0/next@4/key@8/value@16。
    # 每条记录仍会做对象类型身份校验，步长或偏移错误会立即以类型不符暴露。
    stride = process.u32(entry_info["klass"] + 0x100)
    if not 16 <= stride <= 256 or stride < 24:
        raise ValueError("条目步长异常，停止")
    hash_offset, value_offset = 0, 16
    sampled, free_seen, null_values, scanned = [], 0, 0, 0
    walk_total = min(count, WALK_LIMIT)
    index = 0
    while index < walk_total and len(sampled) < SAMPLE_LIMIT:
        batch_entries = min(65536 // stride, walk_total - index)
        batch = batch_entries * stride
        raw = process.read(entry_info["data"] + index * stride, batch)
        for offset_within in range(0, batch, stride):
            scanned += 1
            hash_code = struct.unpack_from("<i", raw, offset_within + hash_offset)[0]
            if hash_code < 0:
                free_seen += 1
                continue
            value = struct.unpack_from("<Q", raw, offset_within + value_offset)[0]
            if not value:
                null_values += 1
                continue
            record, db = read_record(process, value, spec, profile)
            repeat, _ = read_record(process, value, spec, profile)
            if record != repeat:
                raise ValueError("同一条目两次读取结果不一致，停止")
            if spec["kind"] == "character":
                record["equipment_link"] = sample_equipment_ids(process, db)
            sampled.append(record)
            if len(sampled) >= SAMPLE_LIMIT:
                break
        index += batch_entries
    version_second = process.i32(dictionary + offsets["_version"])
    return {
        "count": count,
        "free_count": free_count,
        "scanned_entries": scanned,
        "free_entries_seen": free_seen,
        "null_values_seen": null_values,
        "sampled": sampled,
        "double_read_consistent": True,
        "dictionary_version": [version_first, version_second],
        "version_stable": version_first == version_second,
    }


def process_inventory(process, account, spec, profile):
    last_error = None
    for attempt in range(2):
        try:
            result = walk_once(process, account, spec, profile)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (87, 299):
                raise
            last_error = exc
            continue
        if result["version_stable"]:
            return result
        last_error = ValueError("字典版本在读取期间发生变化")
    raise ValueError(f"库存 {spec['kind']} 两次尝试均未获得稳定快照：{last_error}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise ValueError("需要全新输出文件")
    profile = json.loads((ba_config.EVIDENCE_DIR / "只读字段布局.json").read_text(encoding="utf-8"))
    game_dir = ba_config.game_dir()
    binary = game_dir / "GameAssembly.dll"
    if ROOTCHECK.hash_file(binary) != profile["gameassembly_sha256"]:
        raise ValueError("磁盘游戏版本与布局证据不符，停止")
    types = ROOTCHECK.source_types()
    slots = ROOTCHECK.slots_for_types(binary, {"Nested": types["Nested"]})
    report = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "bounded_runtime_sample",
        "sample_limit_per_inventory": SAMPLE_LIMIT,
        "account_values_exported": True,
        "scope": "少量样本供人工核对，非全量导出",
        "status": "not_started",
    }
    process = None
    try:
        pid = args.pid or find_pid()
        report["process_id"] = pid
        with ACCESS.ReadOnlyProcess(pid) as process:
            if Path(process.image_path()).resolve() != (game_dir / "BlueArchive.exe").resolve():
                raise ValueError("进程映像路径不符")
            module = process.module("GameAssembly.dll")
            if Path(module["path"]).resolve() != binary.resolve():
                raise ValueError("已加载模块与磁盘文件不符")
            account, account_class = ROOTCHECK.resolve_root(process, module, types, slots)
            expected = {field["name"]: field["offset"] for field in types["MXAccount"]["fields"]}
            actual = {field["name"]: field["offset"] for field in process.fields(account_class, len(expected))}
            if actual != expected:
                raise ValueError("运行时账号布局与文件证据不一致")
            inventories = {}
            for spec in INVENTORY_SPECS:
                inventories[spec["kind"]] = process_inventory(process, account, spec, profile)
            report.update(status="sampled", inventories=inventories)
    except Exception as exc:
        import traceback
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    finally:
        if process:
            report.update(bytes_read=process.total_bytes, read_calls=process.read_count, handle_closed=process.handle is None)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] != "sampled":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
