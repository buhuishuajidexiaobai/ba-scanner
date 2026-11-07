# BA 数据导出工具 (ba-data-export)

《蔚蓝档案》Steam 国际服（港澳台服）账号数据**外部只读导出工具**。直接读取游戏客户端进程内已加载的结构化数据，一键导出道具、装备、专属武器与角色养成数据——替代逐页截图 OCR：精确、完整、秒级完成。

> **English**: Read-only exporter for Blue Archive (Steam global/TW server) account data — items, equipment, weapons and character progression — by reading the game's own in-memory structures instead of screenshot OCR.

## 免责声明 / Disclaimer

- 本工具**严格只读**：仅申请 `PROCESS_READ` 权限，不注入、不写入内存、不修改文件、不操作网络、不绕过任何保护。
- 但"只读"**不等于官方允许**：外部读取进程内存仍可能违反游戏用户条款，存在被检测的风险，**使用风险自负**。
- 仅供本人账号的数据备份、盘点与学习研究；**禁止**用于账号交易、作弊、代练或任何损害游戏环境与他人利益的用途。
- 本项目与 Nexon Games / Yostar / Nexon Games Studio 及 SchaleDB 均无关联。

## 功能

| 类别 | 导出字段 |
|---|---|
| 道具 | 实例 ID、物品 ID、精确数量（64 位整数，另含限时道具列表） |
| 装备 | 每条实例：装备 ID、Tier、等级、数量、绑定角色（未绑定堆叠 = 背包库存） |
| 专属武器 | 等级、经验、星级、绑定角色 |
| 角色 | 等级、经验、星级、绊等级、5 项技能等级、装备关联、收藏状态 |

输出：`全量导出_*.json`（完整结构化数据）、`导出_什亭之匣格式_*.json`（可直接导入 [什亭之匣](https://arona.icu) 的库存 JSON）、`全量对账报告_*.json`（与旧 OCR 结果的可选差异对账）。

## 环境要求

- Windows 10/11 x64
- 64 位 Python 3.10+，`pip install -r requirements.txt`（zhconv、pefile）
- .NET 8 Runtime（`dotnet` 命令可用）
- 《蔚蓝档案》已登录并进入大厅

## 快速开始（便携包）

1. 从 Release 下载 `ba-data-export-vX.Y.Z-portable.zip` 解压到任意目录（内置已构建的 Il2CppDumper 运行组件）。
2. 双击 `export_data.bat`，等待完成（约半分钟）。
3. 到 `输出\exports\` 取**带时间戳**的产物。

## 从源码使用

```powershell
git clone https://github.com/buhuishuajidexiaobai/ba-data-export.git
cd ba-data-export
copy config.example.json config.json   # 按需修改 game_dir 等路径
pip install -r requirements.txt
python -I -B ba_data_export\ba_export.py
```

首次使用前需要生成离线证据（自动完成）：`python -I -B ba_data_export\ba_rebuild.py`

## 配置（config.json）

| 字段 | 说明 |
|---|---|
| `game_dir` | 游戏安装目录；留空时自动探测常见 Steam 库路径 |
| `il2cpp_dump_tool_dll` / `probe_dll` | Il2CppDumper 构建产物路径；留空时使用发行包内置 `third_party/` |
| `students_data` | 学生 ID↔名称映射（SchaleDB cn-students）；留空用仓库内置 `data/students.min.json` |
| `reconciliation.ocr_inventory_json` | 可选：ba-inventory-ocr 的什亭之匣 JSON，用于数量对账 |
| `reconciliation.liandu_glob_dir` | 可选：练度扫描结果目录，用于角色对账 |

两个对账项均为可选，未配置时自动跳过，不影响导出。

## 游戏更新后

读取链路绑定游戏构建（GameAssembly.dll 的 SHA-256）。游戏更新后运行：

```powershell
python -I -B ba_data_export\ba_rebuild.py
```

脚本自动重新解析元数据、重试注册地址（上游 Il2CppDumper 的自动识别在本客户端有已知缺陷，脚本按修正偏移优先重试）、刷新 `输出/evidence/` 下的布局与引用证据。旧证据保留在 `输出/版本适配_*/`。

## 工作原理（简述）

1. **离线静态解析**：用 [Il2CppDumper](https://github.com/Perfare/Il2CppDumper)（本地构建禁用了 `LoadLibrary` 回退，不执行目标 DLL）解析 `global-metadata.dat` 与 `GameAssembly.dll`，取得 `MX.GameLogic.DBModel` 下各实体的字段类型、继承与实例偏移，并核对注册表计数一致性。
2. **运行时定位**：在 PE 数据节检索 metadata usage token 定位 `MXAccount.Nested.instance` 类型槽 → 静态字段 → 当前账号单例；逐层核对运行时类名与字段偏移与文件证据一致。
3. **有界只读走读**：按字典真实步长（数组类 `element_size`，本构建为 24；注意该运行时 `FieldInfo` 报告的 Entry 偏移与数组实际布局不一致）分块走读角色/装备/道具/武器四个库存字典，每条记录做对象类型身份校验；字典版本前后一致才接受快照。
4. **导出与对账**：聚合输出 JSON；如配置了 OCR 项目结果则逐条对比差异。

全程约 200KB 读取、数十次校验，无任何写入。

## 边界与限制

- 角色详情页的"最终四维"（体力/攻击/防御/治愈）由客户端按等级/装备/绊实时计算，未包含。
- 爱用品（GearDB）结构已就绪，暂未接入导出流程。
- 上游 Il2CppDumper 支持元数据版本 ≤31；超出需等待上游更新。
- 游戏保护机制阻止读取时工具会失败退出，不存在任何绕过逻辑。

## 致谢

- [Perfare/Il2CppDumper](https://github.com/Perfare/Il2CppDumper)（MIT）— IL2CPP 元数据解析
- [SchaleDB/SchaleDB](https://github.com/SchaleDB/SchaleDB) — 学生 ID/名称映射数据（`data/students.min.json`，快照 2026-11-07）
- [buhuishuajidexiaobai/ba-inventory-ocr](https://github.com/buhuishuajidexiaobai/ba-inventory-ocr) — OCR 前作，本工具的对照与导入格式来源

## License

MIT — 见 [LICENSE](LICENSE)。`third_party/` 内的 Il2CppDumper 构建产物遵循其上游 MIT 许可。
