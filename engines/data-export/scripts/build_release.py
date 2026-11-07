"""生成便携发行包 zip：脚本 + third_party 构建 + 数据 + 文档。

用法：python -I -B scripts\\build_release.py [版本号]
third_party 构建产物取自 config.json 指向的本地构建；Il2CppDumper 上游 LICENSE 自动下载缓存。
"""
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ba_data_export"))


def load_config_module():
    spec = __import__("importlib").util.spec_from_file_location("ba_config", ROOT / "ba_data_export" / "ba_config.py")
    module = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ba_config = load_config_module()
VERSION = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
UPSTREAM_LICENSE = "https://raw.githubusercontent.com/Perfare/Il2CppDumper/v6.7.46/LICENSE"


def copy_tree(source, target):
    for path in source.rglob("*"):
        if "__pycache__" in path.parts or path.is_dir():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    release_dir = ROOT / "输出" / "release"
    stage = release_dir / f"ba-data-export-v{VERSION}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    for name in ("ba_data_export", "tests", "data"):
        copy_tree(ROOT / name, stage / name)
    for name in ("README.md", "LICENSE", "requirements.txt", "config.example.json", "export_data.bat"):
        shutil.copy2(ROOT / name, stage / name)
    static_dir = ba_config.dump_tool_dll().parent
    probe_dir = ba_config.probe_dll().parent
    for name, source in (("Il2CppDumper-Static", static_dir), ("Il2CppDumper-Probe", probe_dir)):
        target = stage / "third_party" / name
        target.mkdir(parents=True)
        for path in source.iterdir():
            if path.is_file() and path.suffix in (".dll", ".json", ".deps.json", ".runtimeconfig.json"):
                shutil.copy2(path, target / path.name)
        license_path = source / "LICENSE-Il2CppDumper"
        if not license_path.is_file():
            license_path.write_bytes(urllib.request.urlopen(UPSTREAM_LICENSE, timeout=30).read())
        shutil.copy2(license_path, target / "LICENSE")
    zip_path = release_dir / f"ba-data-export-v{VERSION}-portable.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage.parent))
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    summary = {
        "version": VERSION,
        "zip": str(zip_path),
        "sha256": digest,
        "bytes": zip_path.stat().st_size,
        "third_party_static": str(static_dir),
        "third_party_probe": str(probe_dir),
    }
    (release_dir / f"ba-data-export-v{VERSION}.sha256.txt").write_text(f"{digest}  ba-data-export-v{VERSION}-portable.zip\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
