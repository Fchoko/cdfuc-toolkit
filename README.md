# cdfuc-toolkit

个人维护的 Python 通用工具集，以 `cdfuc` 包的形式提供路径、文件、归档、字符、日志、网络、系统和时间处理功能。

## 项目结构

```text
cdfuc-toolkit/
├── pyproject.toml
├── requirements.txt
├── README.md
└── src/
    └── cdfuc/
        ├── __init__.py
        ├── archfuc.py
        ├── basefuc.py
        ├── charfuc.py
        ├── logfuc.py
        ├── netfuc.py
        ├── pathfuc.py
        ├── sysfuc.py
        ├── timefuc.py
        └── bin/
            ├── 7z2603-x64/
            └── bz68355-x64/
```

- Python 导入名：`cdfuc`
- 项目和发行包名：`cdfuc-toolkit`
- 版本号以 `pyproject.toml` 的 `[project].version` 为准；`cdfuc.__version__` 从安装元数据读取。

## 环境要求

- Python 3.10 或更高版本
- Git（通过 GitHub 安装时需要）
- 归档引擎和部分系统功能面向 Windows

## 安装

请在目标项目所使用的 Python 环境中运行安装命令。推荐在 PyCharm 的项目 Terminal 中执行，并使用 `python -m pip`，确保包装入当前解释器。

### 从 GitHub 安装完整功能

```powershell
python -m pip install --upgrade "cdfuc-toolkit[all] @ git+https://github.com/Fchoko/cdfuc-toolkit.git"
```

### 只安装基础包

```powershell
python -m pip install --upgrade git+https://github.com/Fchoko/cdfuc-toolkit.git
```

### 更新到 GitHub 最新提交

```powershell
python -m pip install --upgrade --force-reinstall "cdfuc-toolkit[all] @ git+https://github.com/Fchoko/cdfuc-toolkit.git"
```

`--force-reinstall` 会在版本号未变化时仍重新安装 GitHub 当前默认分支的代码。发布新版本时，应先递增 `pyproject.toml` 中的版本号，再提交并推送；通常使用 `--upgrade` 即可更新。

如果要安装指定分支或标签，可在 Git URL 后添加 `@分支名` 或 `@标签名`，例如：

```powershell
python -m pip install --upgrade "cdfuc-toolkit[all] @ git+https://github.com/Fchoko/cdfuc-toolkit.git@master"
```

### 从本地源码安装

在仓库根目录运行：

```powershell
python -m pip install ".[all]"
```

开发时使用可编辑安装，修改源码后无需重复安装：

```powershell
python -m pip install -e ".[all]"
```

只安装部分功能依赖时，选择对应 extra：

```powershell
python -m pip install ".[archive]"
python -m pip install ".[text]"
python -m pip install ".[system]"
python -m pip install ".[tables]"
python -m pip install ".[browser]"
```

也可以安装完整依赖清单：

```powershell
python -m pip install -r requirements.txt
```

## 确认安装和版本

查看 pip 安装信息及安装位置：

```powershell
python -m pip show cdfuc-toolkit
```

查看 Python 实际导入的模块位置和版本：

```powershell
python -c "import cdfuc; print(cdfuc.__file__); print(cdfuc.__version__)"
```

如果输出的模块路径不是当前 Python 环境的 `site-packages`，请检查 PyCharm 运行配置中的 `HONBAKO_CDFUC_DIR` 或 `PYTHONPATH`；它们可能让程序优先导入另一份本地源码。

## 基本用法

```python
from cdfuc import ArchFuc, CharFuc, PathFuc, TimeFuc

print(TimeFuc.current_time())
print(CharFuc.process.convert_numbers("第十一话").text)

# SQLite 查询使用 ? 占位符，并将参数单独传入。
rows = PathFuc.file.load_sqlite(
    "data/example.db",
    "SELECT name FROM items WHERE category = ?",
    ("sample",),
)

# 归档接口示例；归档引擎需要随包提供且面向受支持的系统环境。
archive = ArchFuc(engine="7z")
```

`cdfuc` 中的 SQLite 接口使用 Python 标准库 `sqlite3`，无需额外安装 SQLite Python 包。

## 可选依赖

第三方依赖按功能分组，并在相关功能使用时加载：

| Extra | 依赖用途 |
|---|---|
| `archive` | 归档文件类型识别 |
| `text` | 中文数字转换、剪贴板 |
| `system` | 键鼠控制、Windows 系统接口 |
| `tables` | YAML、Excel 与表格处理 |
| `browser` | Selenium 浏览器自动化 |
| `all` | 安装上述全部可选依赖 |

## 外部程序与资源

- 归档引擎资源位于 `src/cdfuc/bin/`，并作为包数据随发行包安装。
- 浏览器自动化还需要本机安装兼容的浏览器和 WebDriver。
- Windows 专用功能依赖 Windows 系统接口，不保证在其他操作系统上可用。

## 版本发布

1. 更新 `pyproject.toml` 中 `[project].version` 的版本号。
2. 提交并推送代码至 GitHub。
3. 用户通过 Git URL 执行安装或升级命令。

`src/cdfuc/__init__.py` 从安装元数据读取 `cdfuc.__version__`；源码未安装时会显示开发版标记。
