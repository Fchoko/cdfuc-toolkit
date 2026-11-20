import csv
import fnmatch
import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

from .basefuc import DefaultInstance, DelegatedBase, check_path, lazy_import, module
from .charfuc import CharFuc
from .logfuc import LogFuc
from .sysfuc import subprocess_run as run

# ============================== 延迟加载依赖 ==============================

filetype = lazy_import("filetype")
yaml = lazy_import("yaml", pip_name="pyyaml")
pd = lazy_import("pandas", extra_modules=["openpyxl"])
shell = lazy_import("win32com.shell.shell", pip_name="pywin32")
shellcon = lazy_import("win32com.shell.shellcon", pip_name="pywin32")


# ============================== 结果数据结构 ==============================

class WalkRes(NamedTuple):
    """目录遍历结果数据结构"""

    files: list[str]  # 文件路径列表
    dirs: list[str]  # 子目录路径列表
    sizes: dict[str, int] | None = None  # 记录单个文件大小，方便按格式二次筛选统计

    @property
    def file_count(self) -> int:
        """
        获取遍历到的文件总数。

        :return: 文件数量
        """
        return len(self.files)

    @property
    def dir_count(self) -> int:
        """
        获取遍历到的子目录总数。

        :return: 子目录数量
        """
        return len(self.dirs)

    @property
    def total_size(self) -> int | None:
        """
        获取目标目录内的总字节数。

        :return: 整个遍历范围的总字节数
        """
        if self.sizes is None:
            return None
        return sum(self.sizes.values())

    @property
    def human_size(self) -> str | None:
        """
        获取人类可读格式化总大小。

        :return: 格式化后的大小字符串（如 "12.35MB"），若未统计大小则返回 None
        """
        if self.total_size is None:
            return None

        return PathFuc.utility.convert(self.total_size)


# ============================== 核心门面与子模块 ==============================

class PathFuc(DefaultInstance):
    """
    路径 / 文件操作工具门面类

    子模块：
        file      → 文件读写与类型判断
        folder    → 目录递归与清理操作
        utility   → 系统 Shell 与通用路径工具
    """

    file: "_File"
    folder: "_Folder"
    utility: "_Utility"

    file = module(lambda parent: _File(parent))
    folder = module(lambda parent: _Folder(parent))
    utility = module(lambda parent: _Utility(parent))

    def __init__(self, chunk: int = 1024 * 64, decimal: int = 2, encoding: str = "utf-8") -> None:
        """
        初始化 PathFuc 实例。

        :param chunk: 文件读取与哈希计算分块大小（字节）
        :param decimal: 文件大小显示精度（保留小数位）
        :param encoding: 文本与配置默认字符集
        """
        self._chunk = chunk
        self._decimal = decimal
        self._encoding = encoding


class _File(DelegatedBase[PathFuc]):
    """文件操作模块"""

    ARCHIVE_EXTS: set[str] = {
        "7z", "zip", "rar", "tar", "gz", "bz2", "xz", "exe",
    }  # 支持的归档文件扩展名

    IMAGE_EXTS: set[str] = {
        "jpg", "jpeg", "png", "bmp", "gif", "webp", "avif",
    }  # 支持的图片扩展名

    VIDEO_EXTS: set[str] = {
        "mp4", "mkv", "avi", "wmv", "flv", "mov", "webm",
    }  # 支持的视频扩展名

    def hash(
            self,
            path: str | Path,
            algorithm: Literal["md5", "sha1", "sha256", "sha512"] = "md5",
    ) -> str:
        """
        分块计算文件哈希值。

        :param path: 文件路径
        :param algorithm: 哈希算法
        :return: 十六进制哈希字符串
        """
        import hashlib

        p = check_path(path, target="file", error="raise")

        try:
            hasher = hashlib.new(algorithm)  # 创建哈希对象
        except ValueError:
            raise ValueError(f"Unsupported hash algorithm: {algorithm!r}") from None

        chunk_size = getattr(self._parent, "_chunk", 65536)
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):  # 逐块读取文件内容，更新哈希对象
                hasher.update(chunk)

        return hasher.hexdigest()  # 获取计算所得值的十六进制表示

    @staticmethod
    def _check_file_type(
            path: str | Path,
            allowed_exts: set[str],
            fast: bool = False,
            mime_prefix: str | None = None,
            bypass_sniff_exts: set[str] | None = None,
    ) -> bool:
        """
        通用文件格式校验。

        根据 fast 参数选择扩展名快速匹配或文件头嗅探模式。
        非快速模式下优先使用文件内容识别；当嗅探库无法识别时，回退到扩展名判断。

        :param path: 待检测文件路径
        :param allowed_exts: 允许的文件扩展名集合
        :param fast: 是否仅依据扩展名快速判断
        :param mime_prefix: 可选的 MIME 前缀辅助判定
        :param bypass_sniff_exts: 跳过魔数嗅探、直接放行的扩展名集合
        :return: 文件类型是否符合要求
        """
        if not (p := check_path(path, target="file", error="ignore")):
            return False

        ext = p.suffix.lower().lstrip(".")

        if fast:  # 快速模式不读取文件内容，仅检查文件扩展名
            return ext in allowed_exts

        if bypass_sniff_exts and ext in bypass_sniff_exts:  # 某些特殊格式无法可靠地通过文件头识别，例如自解压 EXE
            return True

        try:
            kind = filetype.guess(str(p))
            if kind is not None:
                matched_ext = kind.extension.lower() in allowed_exts  # 文件头成功识别后，优先使用实际检测结果
                matched_mime = mime_prefix is not None and kind.mime.startswith(mime_prefix)  # MIME 前缀仅作为额外的类型判断条件
                return matched_ext or matched_mime
        except (OSError, TypeError, ValueError):  # 文件不存在、格式异常或嗅探失败时回退至扩展名判断
            pass

        return ext in allowed_exts

    def is_archive(self, path: str | Path, fast: bool = False) -> bool:
        """
        判断目标文件是否为支持的归档文件。

        :param path: 待检测文件路径
        :param fast: 是否仅依据扩展名快速判断；False 时尝试魔数嗅探
        :return: 属于支持归档类型返回 True，否则返回 False
        """
        return self._check_file_type(
            path,
            allowed_exts=self.ARCHIVE_EXTS,
            fast=fast,
            bypass_sniff_exts={"exe"},
        )

    def is_image(self, path: str | Path, fast: bool = False) -> bool:
        """
        判断目标文件是否为支持的图片文件。

        :param path: 待检测文件路径
        :param fast: 是否仅依据扩展名快速判断；False 时尝试魔数嗅探
        :return: 属于支持图片类型返回 True，否则返回 False
        """
        return self._check_file_type(
            path,
            allowed_exts=self.IMAGE_EXTS,
            fast=fast,
            mime_prefix="image/",
        )

    def is_video(self, path: str | Path, fast: bool = False) -> bool:
        """
        判断目标文件是否为支持的视频文件。

        :param path: 待检测文件路径
        :param fast: 是否仅依据扩展名快速判断；False 时尝试魔数嗅探
        :return: 属于支持视频类型返回 True，否则返回 False
        """
        return self._check_file_type(
            path,
            allowed_exts=self.VIDEO_EXTS,
            fast=fast,
            mime_prefix="video/",
        )

    @staticmethod
    def _normalize_cell(value: str, convert_id: bool = False) -> str | float:
        """
        解析单元格内容。

        数值字符串自动转换为 float；非数值字符串保持原样。

        :param value: 单元格内容
        :param convert_id: 是否允许类似 "0012" 的编号转换为数值
        :return: 解析后的纯字符串或浮点数值
        """
        value = value.replace("\ufeff", "").strip()

        if not CharFuc.detect.is_number(value):
            return value

        if (
                not convert_id
                and value.startswith("0")
                and len(value) > 1
        ):  # 默认保留编号前导零格式
            return value

        try:
            return float(value)
        except ValueError:
            return value

    def load_config(self, path: str | Path) -> dict:
        """
        加载配置文件（JSON / YAML）。

        :param path: 文件路径
        :return: 配置映射字典；不存在返回 {}
        :raises ValueError: 当扩展名不支持时抛出
        """
        if not (p := check_path(path, target="file", error="warn")):  # 检查文件路径是否存在且为文件
            return {}

        ext = p.suffix.lower().lstrip(".")  # 根据后缀名确定文件格式
        encoding = getattr(self._parent, "_encoding", "utf-8")

        match ext:
            case "json":  # 读取 JSON 文件
                with open(p, "r", encoding=encoding) as f:
                    return json.load(f) or {}

            case "yml" | "yaml":
                with open(p, "r", encoding=encoding) as f:
                    return yaml.safe_load(f) or {}

            case _:
                raise ValueError(f"Unsupported config format: {ext!r}")

    def load_table(self, path: str | Path, drop_empty: bool = False) -> list[list[str | float]]:
        """
        加载二维表格文件。支持 csv、xls、xlsx、txt。

        :param path: 文件路径
        :param drop_empty: 是否剔除空行
        :return: 行列嵌套列表结构
        :raises ValueError: 当表格格式不受支持时抛出
        """
        if not (p := check_path(path, target="file", error="warn")):  # 检查路径有效性
            return []

        ext = p.suffix.lower().lstrip(".")  # 统一提取后缀名
        encoding = getattr(self._parent, "_encoding", "utf-8")
        rows: list[list[Any]] = []

        match ext:
            case "csv":
                with open(p, "r", newline="", encoding=encoding) as f:
                    rows = list(csv.reader(f))
                if drop_empty:  # 过滤掉全为空的行
                    rows = [r for r in rows if any(str(c).strip() for c in r)]

            case "xls" | "xlsx":
                df = pd.read_excel(p, header=None, dtype=str)
                df = df.fillna("")  # 统一缺失值为空字符串，防止行列维度错位
                if drop_empty:  # 剔除全空行（按字符串去空格校验）
                    df = df[df.apply(lambda r: any(str(c).strip() for c in r), axis=1)]
                rows = df.values.tolist()

            case "txt":
                with open(p, "r", encoding=encoding) as f:
                    rows = [line.split() for line in map(str.strip, f) if not drop_empty or line]

            case _:
                raise ValueError(f"Unsupported file format: {ext!r}")

        return [
            [self._normalize_cell(str(cell)) for cell in row]
            for row in rows
        ]

    def load_text(self, path: str | Path, drop_empty: bool = False, strip: bool = False) -> list[str]:
        """
        按行读取文本文件。

        :param path: 文件路径
        :param drop_empty: 是否忽略空行
        :param strip: 是否去除首尾空白字符
        :return: 文本列表
        """
        if not (p := check_path(path, target="file", error="warn")):
            return []

        encoding = getattr(self._parent, "_encoding", "utf-8")
        lines: list[str] = []

        with open(p, "r", encoding=encoding) as f:
            for line in f:
                line = line.rstrip("\r\n")  # 始终去除换行符

                if strip:  # 移除首尾空白符
                    line = line.strip()
                if drop_empty and not line:  # 是否跳过空行
                    continue
                lines.append(line)

        return lines

    def save_config(
            self,
            path: str | Path,
            data: dict,
            mode: Literal["w", "a"] = "a",
            sort: bool = True,
    ) -> None:
        """
        保存配置文件。

        :param path: 保存路径
        :param data: 字典数据
        :param mode: 编辑方式：'w' 覆盖；'a' 合并
        :param sort: 是否对键名排序
        """
        if not isinstance(data, dict) or not data:  # 如果数据为空，不执行写入操作
            return

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)  # 自动创建父目录

        ext = p.suffix.lower().lstrip(".")
        encoding = getattr(self._parent, "_encoding", "utf-8")

        if mode == "a" and p.exists():  # 合并数据，右侧覆盖左侧
            old = self.load_config(p)
            if isinstance(old, dict):
                data = old | data

        match ext:
            case "json":
                with open(p, "w", encoding=encoding) as f:  # 将数据按键字典序排序并写回文件
                    json.dump(data, f, indent=4, ensure_ascii=False, sort_keys=sort)

            case "yml" | "yaml":
                with open(p, "w", encoding=encoding) as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=sort)

            case _:
                raise ValueError(f"Unsupported config type: {ext!r}")

    def save_table(
            self,
            path: str | Path,
            data: Sequence[Sequence[str | int | float]],
            mode: Literal["w", "a"] = "a",
    ) -> None:
        """
        保存二维表格。

        :param path: 保存路径
        :param data: 二维数据
        :param mode: 写入模式：'w' 覆盖；'a' 追加
        """
        if not data or not all(
                isinstance(row, Sequence) and not isinstance(row, (str, bytes))
                for row in data
        ):  # 兼容任意非文本序列协议，杜绝合法行数据静默被吞
            return

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        ext = p.suffix.lower().lstrip(".")
        encoding = getattr(self._parent, "_encoding", "utf-8")

        match ext:
            case "csv":
                with open(p, mode=mode, newline="", encoding=encoding) as f:
                    csv.writer(f).writerows(data)

            case "xls" | "xlsx":
                max_cols = max(len(row) for row in data)  # 补齐列数，保证 DataFrame 正常构造
                rows = [list(row) + [None] * (max_cols - len(row)) for row in data]
                df = pd.DataFrame(rows)

                if mode == "a" and p.exists():
                    old = pd.read_excel(p, header=None, engine="openpyxl")
                    df = pd.concat([old, df], ignore_index=True)
                elif mode != "w":
                    raise ValueError(f"Unsupported mode: {mode!r}")

                df.to_excel(p, index=False, header=False, engine="openpyxl")

            case "txt":
                with open(p, mode=mode, encoding=encoding) as f:
                    f.writelines(" ".join(map(str, row)) + "\n" for row in data)

            case _:
                raise ValueError(f"Unsupported file type: {ext!r}")

    def save_text(
            self,
            path: str | Path,
            content: str | Sequence[str],
            mode: Literal["w", "a"] = "a",
            dedup: bool = False,
            separator: str = "\n",
            newline: bool = True,
    ) -> None:
        """
        保存文本。

        :param path: 保存路径
        :param content: 字符串或字符串序列
        :param mode: 编辑模式：'w' 覆盖；'a' 追加
        :param dedup: 是否逐行比对去重
        :param separator: 元素分隔符
        :param newline: 是否在结尾追加分隔符
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, str):  # 内容统一转为列表
            lines = [content]
        elif isinstance(content, Sequence):
            lines = [str(x) for x in content]
        else:
            raise TypeError("Argument 'content' must be str or Sequence[str]")

        if dedup and p.exists():  # 如果启用去重并且文件存在，读取已存在的所有行
            existing = set(self.load_text(p, strip=True))
            lines = [line for line in lines if line.strip() not in existing]  # 去除重复项

        if not lines:  # 如果没有可写入内容，则直接返回
            return

        text = separator.join(lines)
        if newline:
            text += separator

        encoding = getattr(self._parent, "_encoding", "utf-8")
        with open(p, mode=mode, encoding=encoding) as f:
            f.write(text)


class _Folder(DelegatedBase[PathFuc]):
    """目录操作模块"""

    @staticmethod
    def walk(
            path: str | Path,
            deep: bool = True,
            stat: bool = False,
            sort: Literal["n", "none", "a", "asc", "d", "desc"] = "none",
            patterns: str | Sequence[str] | None = None,
            ignore_dirs: str | Sequence[str] = (),
    ) -> WalkRes:
        """
        高性能目录递归遍历函数。

        :param path: 目标根目录路径
        :param deep: 是否递归深入子目录
        :param stat: 是否同时采集文件大小并统计总字节数
        :param sort: 路径深度排序策略：
                     - 'none' / 'n': 默认无序（按文件系统扫描原生顺序）
                     - 'asc' / 'a': 浅层优先（自顶向下，从根目录到叶子）
                     - 'desc' / 'd': 深层优先（自底向上，从叶子目录到根，清理操作专属）
        :param patterns: 文件名通配符过滤白名单（如 "*.py" 或 ["*.png", "*.jpg"]）
        :param ignore_dirs: 遍历时需要跳过和剪枝的目录名集合（如 [".git", "node_modules", "__pycache__"]）
        :return: WalkRes 命名元组数据对象
        """
        if not (p := check_path(path, target="dir", error="ignore")):
            return WalkRes(files=[], dirs=[])

        if isinstance(ignore_dirs, str):  # 1. 归一化忽略目录集合（全小写，实现 O(1) 快速检索剪枝）
            ignore_set = {ignore_dirs.lower()}
        else:
            ignore_set = {str(d).lower() for d in ignore_dirs}

        if isinstance(patterns, str):  # 2. 归一化通配符列表
            pattern_list = [patterns]
        elif patterns is not None:
            pattern_list = list(patterns)
        else:
            pattern_list = []

        files: list[str] = []
        dirs: list[str] = []
        sizes: dict[str, int] | None = {} if stat else None

        stack: list[str] = [str(p)]  # 使用显式栈代替递归调用，避免深层目录结构导致递归深度限制

        while stack:  # 3. 核心高性能栈遍历与剪枝逻辑
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                if pattern_list:  # 若指定了 patterns，进行通配符匹配过滤
                                    if not any(fnmatch.fnmatch(entry.name, pat) for pat in pattern_list):
                                        continue

                                file_path = entry.path
                                files.append(file_path)

                                if stat and sizes is not None:
                                    try:
                                        sz = entry.stat(follow_symlinks=False).st_size
                                    except OSError:  # 访问异常或权限不足时兜底置 0，保证键完整性
                                        sz = 0
                                    sizes[file_path] = sz


                            elif entry.is_dir(follow_symlinks=False):
                                if entry.name.lower() in ignore_set:  # 目录黑名单剪枝：遇到 node_modules 等深坑直接跳过，拒绝压栈与遍历
                                    continue

                                dir_path = entry.path
                                dirs.append(dir_path)
                                if deep:  # 仅在深度遍历模式下继续压入子目录，非递归模式只返回当前目录的直接子项
                                    stack.append(dir_path)

                        except OSError:  # 单个文件或目录发生权限、链接等访问异常时，跳过当前项，不影响其他目录项继续遍历
                            continue
            except OSError:  # 当前目录无法访问时跳过该目录，继续处理栈中的其他目录
                continue

        sort_mode = str(sort).lower()  # 4. 路径层级深度排序处理
        if sort_mode in ("asc", "a"):
            files.sort(key=lambda x: (len(Path(x).parts), x))
            dirs.sort(key=lambda x: (len(Path(x).parts), x))
        elif sort_mode in ("desc", "d"):
            files.sort(key=lambda x: (-len(Path(x).parts), x))
            dirs.sort(key=lambda x: (-len(Path(x).parts), x))

        return WalkRes(
            files=files,
            dirs=dirs,
            sizes=sizes,
        )

    def clean_empty(self, path: str | Path, deep: bool = False, explorer: bool = True) -> None:
        """
        自底向上级联删除空文件夹。

        :param path: 起始目标目录
        :param deep: 是否深度递归检查
        :param explorer: 是否移入系统回收站
        """
        root_path = Path(path).resolve()
        raw_dirs = self.walk(root_path, deep=deep).dirs

        dir_set = {Path(d).resolve() for d in raw_dirs}  # 统一转绝对路径并利用 dict 去重
        dir_set.add(root_path)

        sorted_dirs = sorted(dir_set, key=lambda pt: len(pt.parts), reverse=True)  # 严格按路径分片深度降序排序（叶子目录先删）

        for p in sorted_dirs:
            if p.exists() and p.is_dir() and not any(p.iterdir()):  # 校验目录存在且为空
                if self._parent.utility.delete(p, explorer=explorer):
                    LogFuc.info(f"Removed empty folder: {str(p)}")
                else:
                    LogFuc.warning(f"Failed to remove empty folder: {str(p)}")

    def clean_pycache(self, path: str | Path, explorer: bool = True) -> int:
        """
        递归清理指定目录下的所有 __pycache__ 目录及其内部编译缓存文件。

        :param path: 起始目标目录
        :param explorer: 是否移入系统回收站（True 移入回收站，False 直接物理删除）
        :return: 成功清理的 __pycache__ 目录总数
        """
        if not (p := check_path(path, target="dir", error="ignore")):
            return 0

        raw_dirs = self.walk(p, deep=True, sort="desc").dirs  # 调用自身的 walk 进行深度递归遍历，获取所有子目录路径
        pycache_dirs = {
            Path(d).resolve() for d in raw_dirs if Path(d).name == "__pycache__"
        }  # 筛选出名字为 __pycache__ 的目录，并转为 Path 对象去重

        if not pycache_dirs:
            return 0

        sorted_pycache_dirs = sorted(pycache_dirs, key=lambda pt: len(pt.parts), reverse=True)  # 严格按路径层级深度降序排序

        count = 0
        for pycache_dir in sorted_pycache_dirs:
            try:
                if pycache_dir.exists() and pycache_dir.is_dir():
                    if self._parent.utility.delete(pycache_dir, explorer=explorer):
                        count += 1
                        LogFuc.info(f"Removed pycache: {str(pycache_dir)}")
                    else:
                        LogFuc.warning(f"Failed to remove pycache: {str(pycache_dir)}")
            except OSError as e:
                LogFuc.error(f"Error removing pycache '{pycache_dir}': {e}")

        return count

    def fix_ext(self, path: str | Path) -> None:
        """
        依据二进制文件头魔数修正目录下与真实格式不符的文件扩展名，并统一将大写后缀规整为小写。

        :param path: 目标目录
        """
        p = check_path(path, target="dir", error="raise")

        for file in self.walk(p, deep=True).files:
            f = Path(file)
            if not (kind := filetype.guess(file)):
                continue

            detected_ext = (
                kind.extension.lower() if kind is not None else f.suffix.lower().lstrip(".")
            )  # 若魔数成功识别则以真实格式为准，否则保留既有扩展名
            if not detected_ext:
                continue

            target_suffix = f".{detected_ext}"
            if f.suffix != target_suffix:  # 只要扩展名类型不符，或者类型一致但原文件为大写（如 .JPG），均触发纠偏
                new_path = f.with_suffix(target_suffix)
                try:
                    os.replace(f, new_path)  # os.replace 可在 Windows NTFS 下平滑支持单纯的大小写重命名
                    LogFuc.info(f"[fix_ext] {f.name} -> {new_path.name}")
                except OSError as e:
                    LogFuc.warning(f"[fix_ext] Failed to rename '{file}': {e}")

    def is_leaf(
            self,
            path: str | Path,
            allow: str | Sequence[str] | None = None,
            ignore: str | Sequence[str] = (),
            fast: bool = False,
    ) -> bool:
        """
        判断目标目录是否为无子目录且文件纯净的叶子目录。

        :param path: 目录路径
        :param allow: 允许的文件扩展名白名单（如 "jpg"）
        :param ignore: 豁免忽略的无害系统文件名（如 "desktop.ini"）
        :param fast: 是否仅依据扩展名快速验证
        :return: 满足叶子条件返回 True，否则返回 False
        """
        if not (p := check_path(path, target="dir", error="ignore")):
            return False

        res = self.walk(p, deep=False)
        if res.dirs:  # 存在子目录
            return False

        ignore_set = {
            x.lower()
            for x in ([ignore] if isinstance(ignore, str) else ignore)
        }  # 忽略的文件名转集合

        valid_files = [
            Path(file)
            for file in res.files
            if Path(file).name.lower() not in ignore_set
        ]  # 清洗待忽略的文件

        if not valid_files:  # 空目录
            return False

        allow_set = {
            x.lower().lstrip(".")
            for x in ([allow] if isinstance(allow, str) else allow or [])
        }  # 允许的拓展名转集合

        if not allow_set:  # 不限制类型情况
            return True

        if fast:  # 快速模式：不解析文件内容，仅逐个校验文件的扩展名是否在允许集合中
            return all(file.suffix.lower().lstrip(".") in allow_set for file in valid_files)

        for file in valid_files:
            ext = file.suffix.lower().lstrip(".")  # 提取基础扩展名作为嗅探失效时的备用回退
            try:
                kind = filetype.guess(str(file))  # 读取文件头进行魔数探测识别
                matched = kind.extension.lower() in allow_set if kind is not None else ext in allow_set
            except (OSError, TypeError, ValueError):  # 文件读取受阻、权限不足或解析抛出异常时回退到扩展名比对
                matched = ext in allow_set

            if not matched:  # 只要存在任意一个非白名单文件，即判定当前目录不满足叶子作品集条件，提前熔断退出
                return False

        return True


class _Utility(DelegatedBase[PathFuc]):
    """通用工具模块"""

    ILLEGAL_CHARS: str = '<>:"/\\|?*'  # Windows 文件名非法字符

    SIZE_UNITS: list[tuple[str, int]] = [
        ("BB", 1024 ** 9),
        ("YB", 1024 ** 8),
        ("ZB", 1024 ** 7),
        ("EB", 1024 ** 6),
        ("PB", 1024 ** 5),
        ("TB", 1024 ** 4),
        ("GB", 1024 ** 3),
        ("MB", 1024 ** 2),
        ("KB", 1024),
        ('B', 1)
    ]  # 文件大小单位和对应的字节数倍数

    def convert(self, byte: int) -> str:
        """
        将字节数转换为人类可读大小字符串。

        :param byte: 字节数值
        :return: 格式化结果（如 "12.35MB"）
        """
        if byte < 0:
            raise ValueError("Size cannot be negative")

        decimal = getattr(self._parent, "_decimal", 2)

        for unit, factor in self.SIZE_UNITS:  # 遍历单位，并确定合适的转换
            converted = byte / factor
            if 1 <= converted < 1024:  # 确保选择最接近的单位
                return f"{round(converted, int(decimal))}{unit}"

        return f"{byte}B"

    @classmethod
    def sanitize(
            cls,
            path: str | Path, mode: Literal["r", "replace", "f", "fullwidth"] = "r",
            repl: str | dict[str, str] = "_"
    ) -> str:
        """
        清洗并纠正文件名中的非法特殊字符及系统保留设备字。

        :param path: 待处理的文件路径或文件名
        :param mode: 清洗策略：'r'/'replace' 替换非法字符；'f'/'fullwidth' 转换为全角字符
        :param repl: 替换字符或映射字典（仅 'r' 模式生效）
        :return: 修正后的合规路径字符串
        :raises ValueError: 当文件名清洗后为空或替换参数非法时抛出
        """
        p = Path(path)
        name = p.name

        if not name.strip():
            raise ValueError(f"Invalid filename: {str(path)!r}")

        if mode.lower() in {"f", "fullwidth"}:  # 转全角模式
            name = name.translate(
                str.maketrans({
                    "<": "＜", ">": "＞", ":": "：", '"': "＂",
                    "/": "／", "\\": "＼", "|": "｜", "?": "？", "*": "＊",
                })
            )
        elif mode.lower() in {"r", "replace"}:
            if isinstance(repl, dict):  # 处理映射字典：单遍字符过滤替换，避免二次正则破坏映射结果
                name = "".join(repl.get(c, ("" if c in cls.ILLEGAL_CHARS else c)) for c in name)
                if any(c in cls.ILLEGAL_CHARS for c in name):
                    raise ValueError("Replacement mapping produced illegal filename characters")
            else:
                if not repl or any(c in cls.ILLEGAL_CHARS for c in repl):
                    raise ValueError(f"Invalid replacement character: {repl!r}")
                name = re.sub(rf"[{re.escape(cls.ILLEGAL_CHARS)}]", repl, name)
        else:
            raise ValueError(f"Unknown sanitize mode: {mode!r}")

        name = name.rstrip(" .")  # Windows 不允许结尾为空格或点
        if not name:  # 不能以空格或点结尾，如 “ .txt”
            raise ValueError("Filename becomes empty after sanitize")

        reserved = {
            "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))
        }  # Windows 保留设备名

        device_name = name.split(".", 1)[0].rstrip(" .").upper()
        if device_name in reserved:  # Windows 设备名在附加一个或多个扩展名后仍然保留
            name = "_" + name

        return str(p.with_name(name))

    @staticmethod
    def _execute_shell(operation: int, src: Path, dst: Path | None = None, *, rename: bool = False) -> bool:
        """
        执行 Windows Shell 文件操作（删除/复制/移动/重命名），支持回收站与 Ctrl+Z 撤销。

        :param operation: shellcon 操作类型（FO_DELETE / FO_COPY / FO_MOVE / FO_RENAME）
        :param src: 源绝对路径
        :param dst: 目标绝对路径
        :param rename: 遇同名冲突时是否交由系统自动序号重命名
        :return: 操作成功与否
        """
        try:
            flags = (
                    shellcon.FOF_SILENT  # 不显示系统原生进度条
                    | shellcon.FOF_NOCONFIRMATION  # 静默不弹二次确认窗
                    | shellcon.FOF_ALLOWUNDO  # 允许移入回收站 / 支持 Ctrl+Z 撤回
            )

            if rename:  # 遇到同名碰撞时允许系统自动序号命名
                flags |= shellcon.FOF_RENAMEONCOLLISION

            p_from = str(src.resolve()) + "\0\0"  # Win32 API 严格要求双空字符 \0\0 封顶缓冲区
            p_to = (str(dst.resolve()) + "\0\0") if dst else None

            res = shell.SHFileOperation(cast(Any, (  # cast 防止 IDE 类型报错
                0,  # 父窗口句柄（0 = 无界面）
                operation,  # 操作类型（FO_COPY / FO_MOVE / FO_DELETE / FO_RENAME）
                p_from,  # 源路径（必须是 \0 结尾的字符串）
                p_to,  # 目标路径（同样需要 \0 结尾，删除时为 None）
                flags,  # 操作标识
                None,  # 名称映射（通常不使用）
                None,  # 进度标题（通常不使用）
            )))

            return res[0] == 0 and not res[1]  # SHFileOperation 返回 0 表示成功，res[1] 为 True 表示用户手动取消了操作
        except (ImportError, OSError, ValueError):
            return False

    @staticmethod
    def _resolve_path(src: str | Path, dst: str | Path, smart: bool) -> tuple[Path, Path]:
        """
        解析并校验源路径与目标路径。

        当 explorer=True（Windows Shell 模式）且源为目录时，Windows Shell 原生就具备将源放入 dst 内部的机制，提前拼接会导致二次嵌套。

        :param src: 源路径（校验必须存在）
        :param dst: 目标路径
        :param smart: 若目标为已存在目录，自动拼接源文件名
        :return: (规范化源 Path, 规范化目标 Path)
        """
        sr = check_path(src, target="any", error="raise")  # 检查源路径是否存在
        ds = Path(dst)

        if smart and ds.exists() and ds.is_dir():  # smart: dst 是目录 → 自动拼接文件名
            ds = ds / sr.name

        return sr, ds

    @classmethod
    def _handle_conflict(
            cls,
            dst: Path,
            *,
            conflict: Literal["w", "warn", "o", "overwrite", "r", "rename"],
            explorer: bool
    ) -> tuple[bool, bool]:
        """
        统一决策目标路径冲突处理策略。

        :param dst: 目标路径
        :param conflict:
            - 'w' / 'warn': 冲突报警终止（默认）
            - 'o' / 'overwrite': 覆盖删除已有目标
            - 'r' / 'rename': 交由 Shell 碰撞自动递增重命名 (例如: file (1).txt)
        :param explorer: 是否通过系统回收站删除
        :return: (是否允许继续操作, 是否启用 Shell 碰撞改名)
        """
        policy_map = {
            "w": "warn", "warn": "warn",
            "o": "overwrite", "overwrite": "overwrite",
            "r": "rename", "rename": "rename",
        }  # 别名映射归一化
        mode = policy_map.get(str(conflict).lower())
        if not mode:
            raise ValueError(f"Unknown conflict policy: {conflict!r}. Expected one of {list(policy_map.keys())}")

        if not dst.exists():  # 目标不存在，无需任何处理
            return True, False

        if mode == "warn":
            LogFuc.warning(f"[conflict] Destination '{dst}' already exists -> aborted")
            return False, False

        if mode == "overwrite":
            LogFuc.info(f"[conflict] Destination '{dst}' exists -> deleting for overwrite")
            if not cls.delete(dst, explorer=explorer):
                LogFuc.error(f"[conflict] Failed to remove destination '{dst}' -> aborted")
                return False, False
            return True, False

        return True, True  # mode == "rename"

    @staticmethod
    def _native_copy(sr: Path, ds: Path) -> None:
        """
        原生 Python 复制执行逻辑（文件或目录）。

        :param sr: 源绝对路径
        :param ds: 目标绝对路径
        """
        if sr.is_dir():  # 递归复制目录
            shutil.copytree(sr, ds, dirs_exist_ok=True)
        else:  # 保留元数据复制单文件
            shutil.copy2(sr, ds)

    @staticmethod
    def _native_move(sr: Path, ds: Path, retries: int = 3) -> None:
        """
        原生 Python 移动执行逻辑（带冲突/锁定重试）。

        :param sr: 源绝对路径
        :param ds: 目标绝对路径
        :param retries: 最大重试次数
        :raises OSError: 当重试耗尽仍然失败时抛出
        """
        last_err: Exception | None = None
        for _ in range(retries):  # 循环重试，应对 Windows 下文件句柄偶发占用
            try:
                shutil.move(str(sr), str(ds))
                return
            except (OSError, PermissionError) as err:
                last_err = err

        if last_err:  # 重试耗尽仍未成功，抛出最后一次捕获的异常
            raise last_err

    @classmethod
    def _transfer(
            cls,
            src: str | Path,
            dst: str | Path,
            *,
            shell_op_name: Literal["FO_COPY", "FO_MOVE"],
            native_executor: Callable[[Path, Path], None],
            smart: bool,
            explorer: bool,
            conflict: Literal["w", "warn", "o", "overwrite", "r", "rename"],
            op_label: str
    ) -> bool:
        """
        文件与目录传输（复制/移动）底层统一中枢，彻底消除参数解析与冲突判定重复代码。

        :param src: 源路径
        :param dst: 目标路径
        :param shell_op_name: shellcon 操作常量名 ("FO_COPY" 或 "FO_MOVE")
        :param native_executor: 原生 Python 降级执行回调函数 (接收 sr, ds)
        :param smart: 目标为目录时是否自动拼接源文件名
        :param explorer: 是否使用 Windows Shell 体系
        :param conflict: 冲突处理策略 ('w'/'warn', 'o'/'overwrite', 'r'/'rename')
        :param op_label: 报错日志标签（如 "Copy", "Move"）
        :return: 操作成功与否
        """
        try:
            sr, ds = cls._resolve_path(src=src, dst=dst, smart=smart)

            continue_flag, rename_flag = cls._handle_conflict(
                dst=ds,
                conflict=conflict,
                explorer=explorer,
            )  # 计算实际目标冲突位置

            if not continue_flag:
                return False

            if explorer:
                operation = getattr(shellcon, shell_op_name)
                return cls._execute_shell(
                    operation=operation,
                    src=sr,
                    dst=ds,
                    rename=rename_flag,
                )

            if rename_flag and ds.exists():
                LogFuc.error("Auto-rename collision conflict is not supported in native Python mode")
                return False

            native_executor(sr, ds)
            return True
        except (OSError, PermissionError, shutil.Error) as e:
            LogFuc.error(f"{op_label} failed from '{src}' to '{dst}': {e}")
            return False

    @classmethod
    def delete(cls, path: str | Path, explorer: bool = True) -> bool:
        """
        删除文件或目录。

        :param path: 目标路径
        :param explorer: 是否使用系统删除（True 移动到回收站，False 直接物理删除）
        :return: 执行结果
        """
        try:
            p = Path(path)

            if explorer:
                return cls._execute_shell(shellcon.FO_DELETE, p)

            if p.is_symlink() or p.is_file():  # 删除文件和快捷方式；symlink 必须优先判断（避免误删目标）
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)

            return True
        except (FileNotFoundError, PermissionError, OSError):
            return False

    @classmethod
    def copy(
            cls,
            src: str | Path,
            dst: str | Path,
            *,
            smart: bool = True,
            explorer: bool = True,
            conflict: Literal["w", "warn", "o", "overwrite", "r", "rename"] = "warn"
    ) -> bool:
        """
        复制文件或目录到指定位置。

        :param src: 源待复制路径
        :param dst: 目标路径
        :param smart: 目标为目录时是否自动拼接源文件名
        :param explorer: 是否使用系统复制（Explorer 行为）
        :param conflict: 冲突处理策略：'w'/'warn' 终止；'o'/'overwrite' 覆盖；'r'/'rename' 自动重命名
        :return: 执行结果
        """
        return cls._transfer(
            src=src,
            dst=dst,
            shell_op_name="FO_COPY",
            native_executor=cls._native_copy,
            smart=smart,
            explorer=explorer,
            conflict=conflict,
            op_label="Copy",
        )

    @classmethod
    def move(
            cls,
            src: str | Path,
            dst: str | Path,
            *,
            smart: bool = True,
            explorer: bool = True,
            conflict: Literal["w", "warn", "o", "overwrite", "r", "rename"] = "warn"
    ) -> bool:
        """
        移动文件或目录到指定位置。

        :param src: 源路径
        :param dst: 目标路径
        :param smart: 目标为目录时是否自动拼接源文件名
        :param explorer: 是否使用系统移动（支持 Ctrl+Z 撤销）
        :param conflict: 冲突处理策略：'w'/'warn' 终止；'o'/'overwrite' 覆盖；'r'/'rename' 自动重命名
        :return: 执行结果
        """
        return cls._transfer(
            src=src,
            dst=dst,
            shell_op_name="FO_MOVE",
            native_executor=cls._native_move,
            smart=smart,
            explorer=explorer,
            conflict=conflict,
            op_label="Move",
        )

    @classmethod
    def rename(
            cls,
            src: str | Path,
            dst: str | Path,
            *,
            explorer: bool = False,
            conflict: Literal["w", "warn", "o", "overwrite", "r", "rename"] = "warn",
    ) -> bool:
        """
        重命名文件或目录（同目录下精准移动）。

        :param src: 源路径
        :param dst: 新路径或新名称
        :param explorer: 是否使用系统重命名（支持撤销）
        :param conflict: 冲突处理策略
        :return: 执行结果
        """
        return cls.move(
            src=src,
            dst=dst,
            smart=False,
            explorer=explorer,
            conflict=conflict,
        )

    @staticmethod
    def open(path: str | Path) -> None:
        """
        在 Windows 资源管理器中打开并定位文件或文件夹。

        :param path: 文件或目录路径
        """
        if p := check_path(path, target="any", error="raise"):
            target_path = str(p.resolve())  # 转为规范的 Windows 绝对路径字符串
            run(["explorer", target_path], async_mode=True)

    @staticmethod
    def locate(path: str | Path) -> None:
        """
        打开所在父级目录，并在窗口中高亮选中指定的文件或文件夹。

        :param path: 需要高亮定位的目标文件或文件夹路径
        """
        if p := check_path(path, target="any", error="raise"):
            target_path = str(p.resolve())
            run(f'explorer /select,"{target_path}"', async_mode=True)  # /select, 后面紧跟路径（注意英文逗号后不要加空格），实现高亮选中效果
