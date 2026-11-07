"""将离线类型证据整理为版本绑定的最小字段布局；不访问进程。"""
import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

SCALARS = {"int": "<i", "long": "<q", "bool": "<?"}
PREFIX = "MX.GameLogic.DBModel."
BASE = PREFIX + "ConsumableItemBaseDB"
TARGETS = {
    "item": (PREFIX + "ItemDB", ("ServerId", "UniqueId", "StackCount")),
    "equipment": (PREFIX + "EquipmentDB", ("ServerId", "UniqueId", "StackCount", "Level", "Exp", "Tier", "BoundCharacterServerId")),
    "character": (PREFIX + "CharacterDB", ("ServerId", "UniqueId", "StarGrade", "Level", "Exp", "FavorRank", "FavorExp", "PublicSkillLevel", "ExSkillLevel", "PassiveSkillLevel", "ExtraPassiveSkillLevel", "LeaderSkillLevel", "IsFavorite")),
    "weapon": (PREFIX + "WeaponDB", ("UniqueId", "Level", "Exp", "StarGrade", "BoundCharacterServerId")),
    "gear": (PREFIX + "GearDB", ("ServerId", "UniqueId", "Level", "Exp", "Tier", "SlotIndex", "BoundCharacterServerId")),
}


def field_name(raw):
    match = re.fullmatch(r"<(.+)>k__BackingField", raw)
    return match.group(1) if match else raw


def build_profile(reports):
    types = {}
    identity = None
    for report in reports:
        current = (report["metadataSha256"], report["binarySha256"])
        if identity is not None and identity != current:
            raise ValueError("不同版本证据不能混用")
        if not report["nativeOffsetsResolved"] or report["processMemoryRead"]:
            raise ValueError("需要有效的离线偏移证据")
        identity = current
        types.update({entry["name"]: entry for entry in report["types"]})
    layouts = {}
    for kind, (type_name, wanted) in TARGETS.items():
        entry = types[type_name]
        parents = [types[BASE]] if kind in ("item", "equipment") else []
        if parents and entry["parentType"] != BASE:
            raise ValueError("物品继承结构不匹配")
        all_fields = {field_name(field["name"]): field for cls in parents + [entry] for field in cls["fields"]}
        fields = {}
        used = set()
        for name in wanted:
            field = all_fields[name]
            if field["isStatic"] or field["type"] not in SCALARS:
                raise ValueError(f"不接受未知或静态字段 {kind}.{name}")
            offset = field["offset"]
            size = struct.calcsize(SCALARS[field["type"]])
            if offset is None or offset < 16 or offset + size > 4096:
                raise ValueError(f"实例字段偏移超出范围 {kind}.{name}")
            extent = set(range(offset, offset + size))
            if used.intersection(extent):
                raise ValueError(f"字段重叠 {kind}.{name}")
            used.update(extent)
            fields[name] = {"offset": offset, "type": field["type"], "size": size}
        layouts[kind] = {
            "type_name": type_name,
            "type_definition_index": entry["typeDefinitionIndex"],
            "minimum_bytes": max(field["offset"] + field["size"] for field in fields.values()),
            "fields": fields,
        }
    return {
        "format": "ba-readonly-layout-v1",
        "status": "offline_verified_runtime_unverified",
        "architecture": "windows-x64-il2cpp",
        "steam_appid": 3557620,
        "metadata_version": 31,
        "native_registration_layout": 29,
        "metadata_sha256": identity[0],
        "gameassembly_sha256": identity[1],
        "layouts": layouts,
        "unresolved": [
            "运行时集合根、对象身份与数据新鲜度",
            "全量覆盖、账号边界与未加载数据",
            "EquipmentServerIds 与 PotentialStats 集合解析",
            "角色详情页体力/攻击/防御/治愈最终计算值",
            "不同客户端构建的兼容性与重启后的定位",
        ],
        "process_memory_read": False,
        "account_data_exported": False,
    }


def decode_record(layout, data):
    """仅解码调用方提供的缓冲区；对象身份须由未来采集层单独验证。"""
    if len(data) < layout["minimum_bytes"]:
        raise ValueError("记录不完整，禁止用零补齐")
    result = {}
    for name, field in layout["fields"].items():
        offset = field["offset"]
        if field["type"] == "bool" and data[offset] not in (0, 1):
            raise ValueError("布尔值无效")
        result[name] = struct.unpack_from(SCALARS[field["type"]], data, offset)[0]
    for name in ("ServerId", "UniqueId", "StackCount", "Level", "Exp", "Tier", "StarGrade", "FavorRank", "FavorExp", "BoundCharacterServerId", "SlotIndex"):
        if name in result and result[name] < 0:
            raise ValueError(f"字段 {name} 为负，拒绝输出")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise ValueError("布局输出文件已存在，拒绝覆盖")
    names = ("目标集合与实体.json", "核心对象与物品基类.json")
    reports = [json.loads((args.analysis_dir / name).read_text(encoding="utf-8")) for name in names]
    profile = build_profile(reports)
    profile["evidence"] = [{"name": name, "sha256": hashlib.sha256((args.analysis_dir / name).read_bytes()).hexdigest()} for name in names]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        json.dump(profile, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": profile["status"], "entities": list(profile["layouts"]), "field_count": sum(len(layout["fields"]) for layout in profile["layouts"].values()), "account_data_exported": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
