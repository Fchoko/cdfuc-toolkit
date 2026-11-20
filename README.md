# cdfuc-toolkit

个人维护的 Python 通用工具集，以 `cdfuc` 包的形式提供归档、文本、路径、日志、网络、系统和时间处理功能。

## 环境要求

- Python 3.10 或更新版本
- Windows 下可使用全部系统集成功能；其他平台的支持取决于所调用的具体功能

## 安装

从 GitHub 安装全部可选依赖：

```powershell
python -m pip install "cdfuc-toolkit[all] @ git+https://github.com/Fchoko/cdfuc-toolkit.git"
```

从克隆的仓库安装：

```powershell
python -m pip install ".[all]"
```

也可以按需安装功能组，例如 `.[archive]`、`.[text]`、`.[system]`、`.[tables]` 或 `.[browser]`。`requirements.txt` 列出全部可选功能的依赖。

## 基本用法

```python
from cdfuc import ArchFuc, CharFuc, PathFuc, TimeFuc

archive = ArchFuc(engine="7z")
print(TimeFuc.current_time())
```

也可以从子模块导入：

```python
from cdfuc.archfuc import ArchFuc
from cdfuc.pathfuc import PathFuc
```

## 模块

| 模块 | 功能 |
|---|---|
| `archfuc` | 压缩、解压、测试和分析归档，支持 7-Zip 与 Bandizip 引擎 |
| `basefuc` | 通用基础工具、代理与延迟导入 |
| `charfuc` | 字符和文本处理 |
| `logfuc` | 日志管理 |
| `netfuc` | Selenium 浏览器自动化 |
| `pathfuc` | 文件、目录、路径及表格操作 |
| `sysfuc` | 系统操作、键鼠控制与命令执行 |
| `timefuc` | 时间处理与计时 |

## 可选依赖和外部程序

- `archive`：归档类型识别
- `text`：中文数字转换和剪贴板
- `system`：键鼠控制及 Windows 系统接口
- `tables`：YAML、Excel 与表格处理
- `browser`：Selenium 浏览器自动化

归档引擎文件随包放在 `cdfuc/bin` 中。浏览器自动化还需要已安装的浏览器；驱动由 Selenium/WebDriver Manager 按运行环境处理。

## 开发

在仓库根目录安装可选依赖并以可编辑模式使用：

```powershell
python -m pip install -e ".[all]"
```
