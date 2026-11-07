"""合成记录与布局构建测试；不读取进程、不依赖真实账号数据。"""
import copy
import importlib.util
import json
import struct
import sys
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "ba_data_export"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mini_evidence.json"


def load(filename):
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), PACKAGE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load("ba_layout_builder.py")


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = [json.loads(FIXTURE.read_text(encoding="utf-8")) for _ in range(2)]
        cls.profile = MODULE.build_profile(cls.reports)

    def make_record(self, kind):
        layout = self.profile["layouts"][kind]
        data = bytearray(layout["minimum_bytes"])
        values = {}
        for index, (name, field) in enumerate(layout["fields"].items(), 1):
            value = True if field["type"] == "bool" else index
            if name == "StackCount":
                value = 2**40 + 123
            values[name] = value
            struct.pack_into(MODULE.SCALARS[field["type"]], data, field["offset"], value)
        return layout, data, values

    def test_all_five_entity_roundtrips(self):
        for kind in MODULE.TARGETS:
            with self.subTest(kind=kind):
                layout, data, values = self.make_record(kind)
                self.assertEqual(MODULE.decode_record(layout, data), values)

    def test_quantity_keeps_int64(self):
        layout, data, _ = self.make_record("item")
        self.assertEqual(MODULE.decode_record(layout, data)["StackCount"], 2**40 + 123)

    def test_base_item_fields_inherited(self):
        for kind in ("item", "equipment"):
            self.assertEqual(self.profile["layouts"][kind]["fields"]["StackCount"]["offset"], 32)

    def test_truncated_record_rejected(self):
        layout, data, _ = self.make_record("character")
        with self.assertRaises(ValueError):
            MODULE.decode_record(layout, data[:-1])

    def test_negative_quantity_rejected(self):
        layout, data, _ = self.make_record("item")
        struct.pack_into("<q", data, layout["fields"]["StackCount"]["offset"], -1)
        with self.assertRaises(ValueError):
            MODULE.decode_record(layout, data)

    def test_invalid_bool_rejected(self):
        layout, data, _ = self.make_record("character")
        data[layout["fields"]["IsFavorite"]["offset"]] = 7
        with self.assertRaises(ValueError):
            MODULE.decode_record(layout, data)

    def test_mixed_versions_rejected(self):
        reports = copy.deepcopy(self.reports)
        reports[1]["binarySha256"] = "0" * 64
        with self.assertRaises(ValueError):
            MODULE.build_profile(reports)

    def test_unknown_field_type_rejected(self):
        reports = copy.deepcopy(self.reports)
        for report in reports:
            for entry in report["types"]:
                if entry["name"] == MODULE.BASE:
                    entry["fields"][2]["type"] = "SecureLong"
        with self.assertRaises(ValueError):
            MODULE.build_profile(reports)

    def test_incomplete_evidence_rejected(self):
        reports = copy.deepcopy(self.reports)
        reports[0]["nativeOffsetsResolved"] = False
        with self.assertRaises(ValueError):
            MODULE.build_profile(reports)

    def test_no_runtime_claim(self):
        self.assertFalse(self.profile["process_memory_read"])
        self.assertFalse(self.profile["account_data_exported"])
        self.assertEqual(self.profile["status"], "offline_verified_runtime_unverified")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
