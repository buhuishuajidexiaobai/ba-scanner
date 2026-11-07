"""只读检查少量白名单存档的结构，不输出字段值或账号前缀。"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

SUFFIXES = (
    "CharacterAccountStudentSortingSaveData.save",
    "CharacterCollectionSortingSaveData.save",
    "CharacterCollectionFilterSaveData.save",
    "EquipmentSortingSaveData.save",
    "ItemInventorySortingSaveData1.save",
    "ItemFilterSaveData.save",
    "FormationPresetSaveData.save",
    "AttachmentSaveData.save",
)


def shape(value, depth=0):
    if isinstance(value, dict):
        if depth >= 3:
            return {"type": "object", "key_count": len(value)}
        return {"type": "object", "keys": {
            key if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,79}", key) else f"<redacted-key-{index}>": shape(child, depth + 1)
            for index, (key, child) in enumerate(value.items())
        }}
    if isinstance(value, list):
        return {"type": "array", "count": len(value), "element_types": sorted({type(item).__name__ for item in value})}
    return {"type": type(value).__name__}


def inspect_cache(root):
    reports = []
    for suffix in SUFFIXES:
        for path in sorted(root.glob("*" + suffix)):
            if path.is_symlink() or not re.fullmatch(r"\d+" + re.escape(suffix), path.name):
                continue
            data = path.open("rb").read(65537)
            report = {"file_suffix": suffix, "bytes": path.stat().st_size}
            if len(data) > 65536:
                report["status"] = "skipped_size_limit"
            else:
                try:
                    value = json.loads(data)
                    report.update(format="json", structure=shape(value))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    report.update(format="unresolved", status="not_parsed_no_raw_bytes_saved")
            reports.append(report)
    db = root / "LocalizeCodeInBuildDBSchema"
    schema = None
    if db.is_file() and not db.is_symlink() and db.open("rb").read(16) == b"SQLite format 3\x00":
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            schema = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
    cache = root / "HTTPCache"
    return {
        "kind": "bounded_cache_structure_inspection_not_account_export",
        "files": reports,
        "localize_database_tables": schema,
        "http_cache_direct_entry_count": len(list(cache.iterdir())) if cache.is_dir() else None,
        "excluded": ["Cookies", "WebCache_*", "Library", "Hosts", "LocalConfig", "GlobalConfig", "SignatureSaveData", "LastConnect", "credentials", "session data"],
        "account_values_exported": False,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = args.out.resolve()
    if output.exists():
        raise ValueError("输出文件已存在")
    report = inspect_cache(args.cache_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
