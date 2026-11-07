# BA Scanner · 蔚蓝档案数据扫描工具箱

三合一开源工具集：把三个《蔚蓝档案》Steam 国际服数据扫描引擎与图形化启动器整合为一个仓库。

| 引擎 | 目录 | 原独立仓库 | 原理 | 耗时 | 精度 |
|---|---|---|---|---|---|
| **内存直读导出（推荐）** | [`engines/data-export`](engines/data-export/) | [ba-data-export](https://github.com/buhuishuajidexiaobai/ba-data-export) | 外部只读游戏进程 | ~30 秒 | 精确（数量/等级原值） |
| **库存 OCR 扫描** | [`engines/inventory-ocr`](engines/inventory-ocr/) | [ba-inventory-ocr](https://github.com/buhuishaujidexiaobai/ba-inventory-ocr) | 截图 + RapidOCR 识别背包 | 数分钟 | 高 |
| **学生练度 OCR 扫描** | [`engines/student-scan`](engines/student-scan/) | [ba-student-scan](https://github.com/buhuishuajidexiaobai/ba-student-scan) | 详情页逐个 OCR + 自动翻页 | 数分钟 | 高 |

图形启动器（`launcher/`）以子进程调用三个引擎，自身不实现扫描逻辑；引擎升级随各引擎源码更新。

> **English**: A combined toolkit for Blue Archive (Steam global server) account data scanning — in-memory read-only export (recommended), inventory OCR and student-progression OCR, with a graphical launcher.

## 目录结构

```
ba-scanner/
├─ launcher/               图形启动器（tkinter，可打包单文件 exe）
│  └─ app/main.py
├─ engines/
│  ├─ data-export/         内存直读引擎（含版本适配 ba_rebuild、测试、SchaleDB 学生映射）
│  ├─ inventory-ocr/       库存 OCR 引擎（采集→映射→什亭之匣 JSON；含 SchaleDB 缓存）
│  └─ student-scan/        练度 OCR 引擎
├─ config.example.json     启动器路径配置模板
├─ requirements.txt        全部引擎的 Python 依赖
└─ LICENSE                 MIT
```

## 快速开始

### 便携包（Release 下载）

1. 从 Releases 下载 `BA扫描工具箱.exe`（图形启动器，无需 Python）。
2. 同仓库下载源码 zip（Code → Download ZIP）解压，把 exe 放进解压目录根部（与 `engines/` 同级），双击即可选方法扫描。

### 从源码运行

```powershell
git clone https://github.com/buhuishuajidexiaobai/ba-scanner.git
cd ba-scanner
copy config.example.json config.json   # 按需覆盖路径
pip install -r requirements.txt
python -I -B launcher\app\main.py
```

各引擎的额外要求见下表：

| 引擎 | 额外要求 |
|---|---|
| 内存直读 | .NET 8 Runtime（`dotnet`）；游戏更新后先跑 `engines\data-export\ba_data_export\ba_rebuild.py` 重新适配 |
| 库存 OCR / 练度 OCR | 游戏**前台**窗口；`requirements.txt` 含 rapidocr/onnxruntime（CPU 亦可，DirectML 可选加速） |

## 各方法准备与产物

- **内存直读**：游戏登录进大厅，全程不用碰游戏。产物：`engines\data-export\输出\exports\`（全量 JSON + 什亭之匣格式）。
- **库存 OCR**：停在背包页；自动置前、翻页、覆盖率自动收工。装备设计图需切「裝備」页再跑一次。**扫描期间勿动鼠标键盘**。产物：`engines\inventory-ocr\输出\`。
- **练度 OCR**：停在学生详情页「基本情報」，自动右方向键翻页收工。产物：`engines\student-scan\输出\`。

## 构建 exe

```powershell
python -I -B -m PyInstaller --onefile --noconsole --name "BA扫描工具箱" --distpath dist --workpath build launcher/app/main.py
```

## 配置

`config.json`（与 `engines/` 同级，缺省全用仓库内置引擎）：

| 字段 | 说明 |
|---|---|
| `memory_tool_dir` 等 | 三引擎目录；默认指向仓库内 `engines/` |
| `memory_python` | 内存导出用的 Python；留空=系统 python |
| `ocr_python` | OCR 引擎解释器；留空=系统 python（需装 requirements） |

## 依赖与免责

- 启动器：Python 3.10+（tkinter 自带）；打包 exe 无 Python 依赖。
- 引擎依赖：见 `requirements.txt`；内存直读另需 .NET 8 Runtime。
- 免责声明：全部引擎**只读**（内存直读仅 `PROCESS_READ`；OCR 仅截屏与翻页）。只读不等于官方允许，存在检测/条款风险，使用风险自负；仅供本人账号备份与学习，禁止用于交易、作弊等用途。与 Nexon/Yostar 无关联。

## 致谢

- [Perfare/Il2CppDumper](https://github.com/Perfare/Il2CppDumper)（MIT）— IL2CPP 元数据解析
- [SchaleDB/SchaleDB](https://github.com/SchaleDB/SchaleDB) — 学生/物品映射数据
- 三个原独立仓库已由本仓库取代，历史版本仍在各自 Releases 可下载。
