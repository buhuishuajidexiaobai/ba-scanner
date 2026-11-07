"""主入口：全量只读取四类库存，导出 JSON，可选与 OCR 项目结果对账；不保存原始内存。"""
import importlib.util
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import zhconv


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCESS = load("ba_process_reader.py")
ROOTCHECK = load("ba_account_root.py")
SAMPLE = load("ba_sample_reader.py")
LAYOUT = load("ba_layout_builder.py")
ba_config = load("ba_config.py")

INVENTORY_SPECS = [
    {"kind": "item", "account_field": "ItemInventory", "inventory_type": "ItemInventoryObject", "wrapper_type": "ItemObject", "db_type": "MX.GameLogic.DBModel.ItemDB"},
    {"kind": "equipment", "account_field": "EquipmentInventory", "inventory_type": "EquipmentInventoryObject", "wrapper_type": "EquipmentObject", "db_type": "MX.GameLogic.DBModel.EquipmentDB"},
    {"kind": "weapon", "account_field": "WeaponListInfo", "inventory_type": "WeaponListObject", "wrapper_type": "WeaponObject", "db_type": "MX.GameLogic.DBModel.WeaponDB"},
    {"kind": "character", "account_field": "CharacterInventory", "inventory_type": "CharacterInventoryObject", "wrapper_type": "CharacterObject", "db_type": "MX.GameLogic.DBModel.CharacterDB"},
]
SPOT_CHECKS = 5


def resolve_inventory(process, account, spec):
    pointer = process.u64(account + SAMPLE.offset_of("MXAccount", spec["account_field"]))
    SAMPLE.verify_class(process, pointer, spec["inventory_type"], "库存对象")
    dictionary = process.u64(pointer + 24)
    SAMPLE.verify_class(process, dictionary, "System.Collections.Generic.Dictionary`2", "库存容器")
    dict_klass = process.u64(dictionary)
    offsets = {field["name"]: field["offset"] for field in process.fields(dict_klass, process.field_count(dict_klass))}
    return dictionary, offsets


def walk_full(process, account, spec, profile, klass_cache):
    dictionary, offsets = resolve_inventory(process, account, spec)
    count = process.i32(dictionary + offsets["_count"])
    free_count = process.i32(dictionary + offsets["_freeCount"])
    version_first = process.i32(dictionary + offsets["_version"])
    entries = process.u64(dictionary + offsets["_entries"])
    if not (0 <= count <= 1_000_000 and 0 <= free_count <= count) or not entries:
        raise ValueError("库存字典计数或指针异常")
    entry_info = process.array_info(entries)
    stride = process.u32(entry_info["klass"] + 0x100)
    if not 16 <= stride <= 256 or stride < 24:
        raise ValueError("条目步长异常")
    layout = profile["layouts"][spec["kind"]]
    db_offset = SAMPLE.offset_of(spec["wrapper_type"], "<DB>k__BackingField")
    records, null_values = [], 0
    index = 0
    while index < count:
        batch_entries = min(65536 // stride, count - index)
        batch = batch_entries * stride
        raw = process.read(entry_info["data"] + index * stride, batch)
        for offset_within in range(0, batch, stride):
            if struct.unpack_from("<i", raw, offset_within)[0] < 0:
                continue
            value = struct.unpack_from("<Q", raw, offset_within + 16)[0]
            if not value:
                null_values += 1
                continue
            record = read_wrapped(process, value, spec, layout, db_offset, klass_cache)
            records.append(record)
        index += batch_entries
    version_second = process.i32(dictionary + offsets["_version"])
    if version_first != version_second:
        raise ValueError(f"{spec['kind']} 字典在遍历期间发生变更")
    return {"count": count, "free_count": free_count, "null_values": null_values, "records": records, "version": version_first}


def read_wrapped(process, wrapper, spec, layout, db_offset, klass_cache):
    ensure_klass(process, process.u64(wrapper), spec["wrapper_type"], klass_cache)
    db = process.u64(wrapper + db_offset)
    ensure_klass(process, process.u64(db), spec["db_type"], klass_cache)
    record = LAYOUT.decode_record(layout, process.read(db, layout["minimum_bytes"]))
    record["record_kind"] = spec["kind"]
    if spec["kind"] == "character":
        list_address = process.u64(db + SAMPLE.offset_of("MX.GameLogic.DBModel.CharacterDB", "<EquipmentServerIds>k__BackingField"))
        record["EquipmentServerIdsFull"] = read_server_id_list(process, list_address)
    return record


def read_server_id_list(process, address):
    if not address:
        return []
    list_klass = SAMPLE.verify_class(process, address, "System.Collections.Generic.List`1", "服务器ID列表")
    offsets = {field["name"]: field["offset"] for field in process.fields(list_klass, process.field_count(list_klass))}
    size = process.i32(address + offsets["_size"])
    if not 0 <= size <= 64:
        raise ValueError("服务器ID列表长度异常")
    items = process.u64(address + offsets["_items"])
    info = process.array_info(items)
    if size == 0:
        return []
    raw = process.read(info["data"], size * 8)
    return [struct.unpack_from("<Q", raw, index * 8)[0] for index in range(size)]


def ensure_klass(process, klass, expected, cache):
    if klass not in cache:
        actual = process.class_name(klass)
        if actual != expected:
            raise ValueError(f"类型不符：期望 {expected}，实际 {actual}")
        cache[klass] = expected


def walk_expiry_items(process, account, profile, klass_cache):
    list_address = process.u64(account + SAMPLE.offset_of("MXAccount", "ExpiryItems"))
    if not list_address:
        return {"count": 0, "records": []}
    list_klass = SAMPLE.verify_class(process, list_address, "System.Collections.Generic.List`1", "限时道具列表")
    offsets = {field["name"]: field["offset"] for field in process.fields(list_klass, process.field_count(list_klass))}
    size = process.i32(list_address + offsets["_size"])
    if not 0 <= size <= 100_000:
        raise ValueError("限时道具列表长度异常")
    items = process.u64(list_address + offsets["_items"])
    info = process.array_info(items)
    layout = profile["layouts"]["item"]
    records = []
    index = 0
    while index < size:
        batch = min(8192, size - index)
        raw = process.read(info["data"] + index * 8, batch * 8)
        for offset_within in range(0, batch * 8, 8):
            db = struct.unpack_from("<Q", raw, offset_within)[0]
            if not db:
                continue
            ensure_klass(process, process.u64(db), "MX.GameLogic.DBModel.ItemDB", klass_cache)
            record = LAYOUT.decode_record(layout, process.read(db, layout["minimum_bytes"]))
            record["record_kind"] = "expiry_item"
            records.append(record)
        index += batch
    return {"count": size, "records": records}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ocr_skill(value, max_level):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    if "MAX" in text:
        return max_level
    if text.isdigit():
        return int(text)
    return None


def ocr_tier(value):
    if value is None:
        return None
    text = str(value).strip().upper().lstrip("T")
    return int(text) if text.isdigit() else None


def multiset_diff(ours, theirs):
    ours_counts, theirs_counts = {}, {}
    for entry in ours:
        ours_counts[entry] = ours_counts.get(entry, 0) + 1
    for entry in theirs:
        theirs_counts[entry] = theirs_counts.get(entry, 0) + 1
    keys = set(ours_counts) | set(theirs_counts)
    return [{"item": list(key), "ours": ours_counts.get(key, 0), "ocr": theirs_counts.get(key, 0)} for key in sorted(keys) if ours_counts.get(key, 0) != theirs_counts.get(key, 0)]


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = (ba_config.EXPORTS_DIR / f"全量对账报告_{stamp}.json").resolve()
    if out.exists():
        raise ValueError("输出文件已存在")
    profile = load_json(ba_config.EVIDENCE_DIR / "只读字段布局.json")
    game_dir = ba_config.game_dir()
    binary = game_dir / "GameAssembly.dll"
    if ROOTCHECK.hash_file(binary) != profile["gameassembly_sha256"]:
        raise SystemExit("磁盘游戏版本与布局证据不符——游戏更新后请先运行 ba_rebuild.py 重新适配")

    ocr_path = ba_config.ocr_inventory_json()
    ocr_inventory = load_json(ocr_path) if ocr_path else None
    ocr_items = {int(key[5:]): value for key, value in (ocr_inventory or {}).items() if key.startswith("item_")}
    ocr_equipment = {int(key[10:]): value for key, value in (ocr_inventory or {}).items() if key.startswith("equipment_")}
    liandu_dir = ba_config.liandu_glob_dir()
    liandu_candidates = sorted(liandu_dir.glob("练度扫描_*_raw.json")) if liandu_dir else []
    liandu = load_json(liandu_candidates[-1]) if liandu_candidates else None
    students_path = ba_config.students_data()
    students_raw = load_json(students_path) if students_path else {}
    item_names, equipment_names = {}, {}
    if ocr_path:
        cache_dir = ocr_path.parent.parent / "cache"
        for filename, target in (("tw_items.min.json", item_names), ("tw_equipment.min.json", equipment_names)):
            source = cache_dir / filename
            if source.is_file():
                target.update({entry["Id"]: entry.get("Name") for entry in load_json(source).values()})
    name_to_ids = {}
    for student_id, entry in students_raw.items():
        name = entry.get("Name")
        if name:
            name_to_ids.setdefault(name, []).append(int(student_id))

    report = {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "stage": "full_reconciliation_readonly",
        "ocr_inventory_mtime": datetime.fromtimestamp(os.path.getmtime(ocr_path)).isoformat(timespec="seconds") if ocr_path else None,
        "liandu_file": liandu_candidates[-1].name if liandu_candidates else None,
        "liandu_time": (liandu or {}).get("meta", {}).get("time"),
        "account_values_exported": True,
        "scope": "全量只读读取用于导出与 OCR 对账",
        "status": "not_started",
    }
    klass_cache = {}
    process = None
    try:
        pid = SAMPLE.find_pid()
        report["process_id"] = pid
        with ACCESS.ReadOnlyProcess(pid, max_bytes=8_000_000, max_seconds=180) as process:
            if Path(process.image_path()).resolve() != (game_dir / "BlueArchive.exe").resolve():
                raise ValueError("进程映像路径不符")
            module = process.module("GameAssembly.dll")
            if Path(module["path"]).resolve() != binary.resolve():
                raise ValueError("已加载模块与磁盘文件不符")
            account, account_class = ROOTCHECK.resolve_root(process, module, SAMPLE.TYPES, ROOTCHECK.slots_for_types(binary, {"Nested": SAMPLE.TYPES["Nested"]}))
            expected = {field["name"]: field["offset"] for field in SAMPLE.TYPES["MXAccount"]["fields"]}
            actual = {field["name"]: field["offset"] for field in process.fields(account_class, len(expected))}
            if actual != expected:
                raise ValueError("运行时账号布局与文件证据不一致")
            inventories = {}
            for spec in INVENTORY_SPECS:
                inventories[spec["kind"]] = walk_full(process, account, spec, profile, klass_cache)
            expiry = walk_expiry_items(process, account, profile, klass_cache)
            report["inventories"] = {
                kind: {key: value for key, value in data.items() if key != "records"} for kind, data in inventories.items()
            }
            report["expiry_items_count"] = expiry["count"]

            item_totals = {}
            for record in inventories["item"]["records"] + expiry["records"]:
                item_totals[record["UniqueId"]] = item_totals.get(record["UniqueId"], 0) + record["StackCount"]
            if ocr_inventory is not None:
                item_diffs = []
                for item_id in sorted(set(item_totals) | set(ocr_items)):
                    ours = item_totals.get(item_id, 0)
                    theirs = ocr_items.get(item_id, 0)
                    if ours != theirs:
                        item_diffs.append({"id": item_id, "name": item_names.get(item_id), "ours": ours, "ocr": theirs, "delta": ours - theirs})
                report["item_diffs"] = item_diffs
            else:
                report["item_diffs"] = None

            equipment_totals = {}
            bound_equipment = {}
            for record in inventories["equipment"]["records"]:
                if record["BoundCharacterServerId"] == 0:
                    equipment_totals[record["UniqueId"]] = equipment_totals.get(record["UniqueId"], 0) + record["StackCount"]
                else:
                    bound_equipment.setdefault(record["BoundCharacterServerId"], []).append((record["Tier"], record["Level"]))
            if ocr_inventory is not None:
                equipment_diffs = []
                for equipment_id in sorted(set(equipment_totals) | set(ocr_equipment)):
                    ours = equipment_totals.get(equipment_id, 0)
                    theirs = ocr_equipment.get(equipment_id, 0)
                    if ours != theirs:
                        equipment_diffs.append({"id": equipment_id, "name": equipment_names.get(equipment_id), "ours": ours, "ocr": theirs, "delta": ours - theirs})
                report["equipment_diffs"] = equipment_diffs
            else:
                report["equipment_diffs"] = None
                report["reconciliation_note"] = "未配置 OCR 库存文件，跳过道具/装备对账；导出不受影响"

            weapon_by_character = {}
            for record in inventories["weapon"]["records"]:
                if record.get("BoundCharacterServerId"):
                    weapon_by_character[record["BoundCharacterServerId"]] = record

            export = {}
            for unique_id, count in sorted(item_totals.items()):
                export[f"item_{unique_id}"] = count
            for unique_id, count in sorted(equipment_totals.items()):
                export[f"equipment_{unique_id}"] = count
            export_path = ba_config.EXPORTS_DIR / f"导出_什亭之匣格式_{stamp}.json"
            export_path.write_text(json.dumps(export, ensure_ascii=False, indent=1), encoding="utf-8")
            report["export_path"] = str(export_path)

            full_export = {
                "meta": {
                    "generated_utc": datetime.now(timezone.utc).isoformat(),
                    "steam_appid": 3557620,
                    "gameassembly_sha256": profile["gameassembly_sha256"],
                    "source": "external_readonly_process_read",
                    "process_memory_written": False,
                    "note": "角色最终四维属性为客户端按等级/装备/绊实时计算，未包含",
                },
                "items": [{"serverId": r["ServerId"], "uniqueId": r["UniqueId"], "count": r["StackCount"]} for r in inventories["item"]["records"]],
                "expiryItems": [{"serverId": r["ServerId"], "uniqueId": r["UniqueId"], "count": r["StackCount"]} for r in expiry["records"]],
                "equipment": [{"serverId": r["ServerId"], "uniqueId": r["UniqueId"], "count": r["StackCount"], "level": r["Level"], "tier": r["Tier"], "boundCharacterServerId": r["BoundCharacterServerId"]} for r in inventories["equipment"]["records"]],
                "weapons": [{"uniqueId": r["UniqueId"], "level": r["Level"], "exp": r["Exp"], "starGrade": r["StarGrade"], "boundCharacterServerId": r["BoundCharacterServerId"]} for r in inventories["weapon"]["records"]],
                "characters": [{
                    "serverId": r["ServerId"],
                    "uniqueId": r["UniqueId"],
                    "name": students_raw.get(str(r["UniqueId"]), {}).get("Name"),
                    "starGrade": r["StarGrade"],
                    "level": r["Level"],
                    "exp": r["Exp"],
                    "favorRank": r["FavorRank"],
                    "favorExp": r["FavorExp"],
                    "skillLevels": {"public": r["PublicSkillLevel"], "ex": r["ExSkillLevel"], "passive": r["PassiveSkillLevel"], "extraPassive": r["ExtraPassiveSkillLevel"], "leader": r["LeaderSkillLevel"]},
                    "isFavorite": r["IsFavorite"],
                    "equipmentServerIds": r.get("EquipmentServerIdsFull") or [],
                } for r in inventories["character"]["records"]],
            }
            full_path = ba_config.EXPORTS_DIR / f"全量导出_{stamp}.json"
            full_path.write_text(json.dumps(full_export, ensure_ascii=False, indent=1), encoding="utf-8")
            report["full_export_path"] = str(full_path)

            character_by_id = {record["UniqueId"]: record for record in inventories["character"]["records"]}

            base_name_to_ids = {}
            for name, ids in name_to_ids.items():
                base = name.split("(")[0].strip()
                if base and base != name:
                    base_name_to_ids.setdefault(base, set()).update(ids)

            def ours_features(record):
                weapon = weapon_by_character.get(record["ServerId"])
                return {
                    "level": record["Level"],
                    "stars": record["StarGrade"],
                    "bond": record["FavorRank"],
                    "ex": record["ExSkillLevel"],
                    "base": record["PublicSkillLevel"],
                    "enh": record["PassiveSkillLevel"],
                    "sub": record["ExtraPassiveSkillLevel"],
                    "weapon_level": weapon["Level"] if weapon else None,
                    "weapon_stars": weapon["StarGrade"] if weapon else None,
                    "gear_count": len(bound_equipment.get(record["ServerId"], [])),
                }

            def row_features(row):
                skills = row.get("skills") or {}
                weapon = row.get("weapon") or {}
                gear = [slot for slot in (row.get("gear") or []) if isinstance(slot, dict) and ocr_tier(slot.get("tier")) is not None]
                return {
                    "level": row.get("level"),
                    "stars": row.get("stars"),
                    "bond": row.get("bond"),
                    "ex": ocr_skill(skills.get("ex"), 5),
                    "base": ocr_skill(skills.get("base"), 10),
                    "enh": ocr_skill(skills.get("enh"), 10),
                    "sub": ocr_skill(skills.get("sub"), 10),
                    "weapon_level": weapon.get("level"),
                    "weapon_stars": weapon.get("stars"),
                    "gear_count": len(gear),
                }, gear

            def feature_score(ocr_feats, ours_feats):
                total = matched = 0
                for key, value in ocr_feats.items():
                    if value is None:
                        continue
                    total += 1
                    if value == ours_feats[key]:
                        matched += 1
                return matched, total

            def best_scored(ocr_feats, candidate_ids, require_ratio):
                best, best_key, ambiguous = None, None, False
                for uid in candidate_ids:
                    if uid in used_unique_ids:
                        continue
                    matched, total = feature_score(ocr_feats, ours_features(character_by_id[uid]))
                    if total < 4 or matched < max(4, total - 1):
                        continue
                    key = (matched, total)
                    if best_key is None or key > best_key:
                        best, best_key, ambiguous = uid, key, False
                    elif key == best_key:
                        ambiguous = True
                if best is not None and not ambiguous and best_key[0] / best_key[1] >= require_ratio:
                    return best
                return None

            character_diffs, unmatched_ocr, matched_count = [], [], 0
            match_methods = {}
            used_unique_ids = set()
            for row in ((liandu or {}).get("rows") or []):
                ocr_feats, ocr_gear = row_features(row)
                student_id, method = None, None
                ocr_name = row.get("name")
                if ocr_name:
                    simplified = zhconv.convert(str(ocr_name).strip(), "zh-hans")
                    for key in (simplified, simplified.split("(")[0].strip(), str(ocr_name).strip()):
                        ids = [uid for uid in (name_to_ids.get(key) or base_name_to_ids.get(key) or []) if uid in character_by_id]
                        if ids:
                            if len(ids) == 1 and ids[0] not in used_unique_ids:
                                student_id = ids[0]
                                method = "name"
                            else:
                                student_id = best_scored(ocr_feats, ids, 0.6)
                                if student_id is not None:
                                    method = "name+features"
                            break
                if student_id is None:
                    student_id = best_scored(ocr_feats, list(character_by_id), 0.75)
                    if student_id is not None:
                        method = "features"
                if student_id is None:
                    unmatched_ocr.append({"name": ocr_name, **{key: value for key, value in ocr_feats.items() if value is not None}})
                    continue
                used_unique_ids.add(student_id)
                matched_count += 1
                match_methods[method] = match_methods.get(method, 0) + 1
                ours = character_by_id[student_id]
                diffs = {}
                for key, field in (("level", "Level"), ("stars", "StarGrade"), ("bond", "FavorRank"), ("ex", "ExSkillLevel"), ("base", "PublicSkillLevel"), ("enh", "PassiveSkillLevel"), ("sub", "ExtraPassiveSkillLevel")):
                    if ocr_feats[key] is not None and ocr_feats[key] != ours[field]:
                        diffs[key] = {"ours": ours[field], "ocr": ocr_feats[key]}
                weapon = weapon_by_character.get(ours["ServerId"])
                if weapon is not None and ocr_feats["weapon_level"] is not None and ocr_feats["weapon_level"] != weapon["Level"]:
                    diffs["weapon_level"] = {"ours": weapon["Level"], "ocr": ocr_feats["weapon_level"]}
                if weapon is not None and ocr_feats["weapon_stars"] is not None and ocr_feats["weapon_stars"] != weapon["StarGrade"]:
                    diffs["weapon_stars"] = {"ours": weapon["StarGrade"], "ocr": ocr_feats["weapon_stars"]}
                ocr_gear_pairs = [(ocr_tier(slot.get("tier")), slot.get("level")) for slot in ocr_gear]
                ocr_gear_pairs = [pair for pair in ocr_gear_pairs if pair[0] is not None and isinstance(pair[1], int)]
                gear_diff = multiset_diff(bound_equipment.get(ours["ServerId"], []), ocr_gear_pairs)
                if gear_diff:
                    diffs["gear"] = gear_diff
                if diffs:
                    character_diffs.append({"unique_id": student_id, "name": students_raw.get(str(student_id), {}).get("Name"), "method": method, "diffs": diffs})
            unmatched_ours = [
                {"unique_id": uid, "name": students_raw.get(str(uid), {}).get("Name"), "level": record["Level"], "stars": record["StarGrade"]}
                for uid, record in character_by_id.items() if uid not in used_unique_ids
            ]
            report.update(
                status="reconciled",
                character_matched=matched_count,
                match_methods=match_methods,
                character_diffs=character_diffs,
                unmatched_ocr_rows=unmatched_ocr,
                unmatched_ours=unmatched_ours,
                character_note=None if liandu is not None else "未配置练度扫描结果，跳过角色对账",
            )
    except Exception as exc:
        import traceback
        report.update(status="stopped", error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
    finally:
        if process:
            report.update(bytes_read=process.total_bytes, read_calls=process.read_count, handle_closed=process.handle is None)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    summary = {
        "status": report["status"],
        "item_diffs": len(report.get("item_diffs") or []),
        "equipment_diffs": len(report.get("equipment_diffs") or []),
        "character_matched": report.get("character_matched"),
        "character_diffs": len(report.get("character_diffs") or []),
        "unmatched_ocr": len(report.get("unmatched_ocr_rows") or []),
        "unmatched_ours": len(report.get("unmatched_ours") or []),
        "counts": report.get("inventories"),
        "expiry_items": report.get("expiry_items_count"),
        "bytes_read": report.get("bytes_read"),
        "export_path": report.get("export_path"),
        "full_export_path": report.get("full_export_path"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if report["status"] != "reconciled":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
