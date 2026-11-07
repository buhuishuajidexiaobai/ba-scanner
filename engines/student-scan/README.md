# 蔚蓝档案学生练度扫描器 (BA Student Scan)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![OCR Engine](https://img.shields.io/badge/OCR-RapidOCR-brightgreen.svg)](https://github.com/RapidAI/RapidOCR)
[![Blue Archive](https://img.shields.io/badge/Game-Blue%20Archive-00bcd4.svg)](https://bluearchive.nexon.com/)

基于 **RapidOCR** 本地离线文字识别与键盘/屏幕自动化，在《蔚蓝档案》**学生详情页**自动按右方向键逐个切换学生、扫描全部练度信息，直到翻回第一个学生自动收工，一键导出 TXT 练度总表 + JSON 原始数据。告别一个个点开学生页面手动记录等级/星级/技能/装备的繁琐盘点！

> [!WARNING]
> ### 📢 郑重提示（免费开源·严禁倒卖）
> - 本项目为**完全免费且开源**的个人辅助小工具，仅供个人学习交流与数据备份使用。
> - **如果您是在闲鱼、淘宝、拼多多、QQ 群等平台付费购买了本软件或打包脚本，说明您已被骗！请立即向平台申请全额退款并举报不良倒卖商家！**

> [!NOTE]
> ### 💻 测试环境
> - **游戏**：《蔚蓝档案》Steam 官方 PC 客户端 — 港澳台服（繁体中文版）
> - **系统**：Windows 11（64 位）
> - **分辨率**：2560 × 1600 全屏（游戏 UI 等比缩放，其他分辨率理论通用，未逐一验证）
> - **实测规模**：153 名学生完整循环扫描，约 8 分钟（DirectML GPU 加速，约 3 秒/学生）

---

## ✨ 核心特性

- 🛡️ **安全零风险**：纯键盘模拟（右方向键）+ 屏幕截图 + 本地离线 OCR，**不注入内存**、**不篡改客户端**、**不读取网络通信**。
- 🔄 **自动循环扫描**：自动切换学生逐屏识别，按「名字 + 绊 + 等级」指纹检测回到第一个学生即自动停止；列表尾部不循环时（连续按键无画面变化）也会自动兜底收工。
- 🚀 **GPU 加速**：自动检测 DirectML（DX12）显卡推理，无独显自动回退 CPU。
- 🔍 **抗 OCR 抖动设计**（153 名学生三轮实测打磨）：
  - 绊/技能/名字/等级小字全屏漏检时自动**局部放大二次识别**；
  - 繁简漂移兼容（OCR 常把「強化技能+」识别成简体）、label+数字合并 token 解析（`防力 377`）、`T2` 角标误读为 `12` 的纠错；
  - 星级用 HSV 颜色连通域计数（金黄=学生星、蓝=武器星），不依赖 OCR。
- 📦 **双格式输出**：排版 TXT（人读总表）+ JSON（含每屏 OCR 原始行，便于排查与二次处理）。
- ⏸️ **随时可停**：Ctrl+C 中断保留已扫描部分。

## 📊 识别字段

| 分组 | 字段 |
|---|---|
| 基础 | 学生名 / 等级 / 星级(★1-5) / 绊(好感) / 定位(STRIKER/SPECIAL) |
| 能力值 | 体力 / 攻击 / 防御 / 治愈 / 能力解放等级 |
| 技能 | EX技能 / 基本技能 / 强化技能+ / 子技能（MAX 或 Lv.N） |
| 武器 | 类型(MG/AR/...) / 等级 / 星 |
| 装备 | 4 槽 Tier(T1-T10) / 等级（空槽记「空」） |

实测准确率（153 名学生）：**四维 / 星级 / 定位 / 技能 0 漏检**；等级漏 2、绊漏 8、名字漏 22（多为单字名等 OCR 检测边界，均以 `—` 标出，可按序号+绊对照游戏人工补录）。

## 🛠️ 获取与运行方式

### 方式一：免安装便携版（推荐·解压即用）
1. 前往本仓库的 [Releases 发行版页面](https://github.com/buhuishuajidexiaobai/ba-student-scan/releases)；
2. 下载最新的 `ba-student-scan-vX.Y.Z-portable.zip`（内嵌完整依赖与轻量 Python 运行环境，支持 DirectML GPU 硬件加速）；
3. 解压到任意目录；
4. 双击 **`启动扫描.bat`**，按提示操作即可，无需额外安装任何软件。

### 方式二：源码运行版（适合开发者）
- **系统要求**：Windows 10 / 11、Python 3.10+
- **安装依赖**（requirements.txt 为实测锁定版本）：
  ```bash
  git clone https://github.com/buhuishuajidexiaobai/ba-student-scan.git
  cd ba-student-scan
  pip install -r requirements.txt
  ```
- GPU 加速（可选）：
  ```bash
  pip uninstall onnxruntime
  pip install onnxruntime-directml==1.24.4
  ```

## 🚀 使用指南

### 步骤 1：打开游戏并停在学生详情页
1. 启动《蔚蓝档案》游戏客户端；
2. 进入 **學生** 列表 → 点开**第一个学生** → 详情页停在 **「基本情報」** 页签；
   > 💡 扫描起点 = 当前显示的学生，之后按右方向键的顺序即扫描顺序（默认列表排序）。

### 步骤 2：启动扫描
- 双击 **`启动扫描.bat`**，按提示按任意键开始；
- 脚本自动激活游戏窗口 → 逐个切换识别 → 完成后结果写入 `输出\` 文件夹：
  - `练度扫描_时间戳.txt` —— 排版好的练度总表
  - `练度扫描_时间戳_raw.json` —— 含解析字段与每屏 OCR 原始行
  - `练度扫描_时间戳_日志.txt` —— 运行日志
- 中途想停：控制台 **Ctrl+C**，已扫描部分照常保存。
- ⚠️ 扫描期间请勿最小化/遮挡游戏窗口、勿移动焦点。

### 命令行参数

```
python ba_student_scan.py --once           # 单帧调试: 不按键, 识别当前画面并打印解析结果+OCR原始行
python ba_student_scan.py --max 500        # 学生数上限 (默认 400 防呆)
python ba_student_scan.py --miss-limit 3   # 按键无反应容忍次数 (默认 3)
python ba_student_scan.py --debug-frames   # 每个学生另存截图到 输出/debug (排查识别错误用)
```

## 📌 已知边界

1. **武器 `Lv.—`**：面板本身不显示等级（武器 0 星未突破状态），并非识别失败；个别装备槽 `Tier —` 同理；
2. **能力解放等级**为黄底小徽章，识别率较低（多为 `—`），仅供参考；
3. 学生名偶发 OCR 错字/截断（如括号不闭合、单字名漏检），重要名字请对照 `输出\debug` 截图或游戏内核对；
4. 需要游戏窗口可见（不能被完全遮挡或最小化）；
5. 游戏版本更新若改版学生详情页 UI，相对坐标需按 `--once` 输出的 OCR 行重新校准（见下方维护说明）。

## 🔧 识别规则维护（接手开发者）

- 全部解析规则集中在 `ba_student_scan.py` 的 `parse_profile()`，区域相对坐标注释均对应 2560×1600 实测值；
- 校准技巧：跑 `--once` 会打印 OCR 全部行（cx | cy | text）并存 debug 截图，对照调整坐标带即可；
- 星级计数参数：HSV 阈值 + 连通域面积窗，可对历史 debug 截图离线扫描调参。

## 📂 仓库文件说明

```text
ba-student-scan/
├── ba_student_scan.py    # 核心扫描器（窗口锚定截图、RapidOCR 引擎、练度解析、循环停止判定）
├── 启动扫描.bat          # 一键运行（便携版自动使用内嵌 runtime）
├── requirements.txt      # Python 依赖清单（实测锁定版本）
├── 输出/                  # 扫描结果（自动生成，不入库）
└── README.md
```

---

## 📜 免责声明 (Disclaimer)

- 本项目为非商业性质的开源工具，仅供个人学习、游戏体验优化与数据备份参考；
- 本工具不涉及修改游戏内存、不篡改通信协议，纯粹基于屏幕截图与键盘模拟；
- 请合理使用本工具，使用者因使用本工具所产生的一切后果由使用者自行承担；
- 《蔚蓝档案》（Blue Archive）游戏相关素材与版权归原版权所有方（NEXON Games / Yostar / 悠星网络 / 艾思维特等）所有。

## 🔗 姊妹项目

- [ba-inventory-ocr](https://github.com/buhuishuajidexiaobai/ba-inventory-ocr) —— 蔚蓝档案**道具库存**自动扫描与什亭之匣导入工具（同一技术栈）
