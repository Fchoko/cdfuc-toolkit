import os
import re
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple

from .basefuc import (
    ClassProxy, DefaultInstance, check_path, lazy_import,
)
from .logfuc import LogFuc
from .pathfuc import PathFuc
from .sysfuc import subprocess_run as run

# ============================== 延迟加载依赖 ==============================

filetype = lazy_import("filetype")


# ============================== 结果数据结构 ==============================

class ArchRes(NamedTuple):
    """
    压缩包解析结果数据结构。
    """

    package: str  # 压缩包名称或绝对路径
    files: list[str]  # 压缩包内所有文件相对路径列表
    orig_sizes: dict[str, int] | None = None  # 文件路径与其原始字节大小的映射
    comp_sizes: dict[str, int] | None = None  # 文件路径与其压缩后字节大小的映射

    @property
    def file_count(self) -> int:
        """
        获取压缩包内包含的文件总数。

        :return: 文件数量
        """
        return len(self.files)

    @property
    def total_orig(self) -> int | None:
        """
        所有文件解压前的原始总字节数。

        :return: 原始总字节数，若未解析大小则返回 None
        """
        if self.orig_sizes is None:
            return None
        return sum(self.orig_sizes.values())

    @property
    def total_comp(self) -> int | None:
        """
        所有文件压缩后的总字节数。

        :return: 压缩后总字节数，若未解析大小则返回 None
        """
        if self.comp_sizes is None:
            return None
        return sum(self.comp_sizes.values())

    @property
    def human_orig(self) -> str | None:
        """
        获取原始总大小的人类可读格式化字符串。

        :return: 格式化结果（如 "12.35MB"），无数据时返回 None
        """
        if self.total_orig is None:
            return None
        return PathFuc.utility.convert(self.total_orig)

    @property
    def human_comp(self) -> str | None:
        """
        获取压缩后总大小的人类可读格式化字符串。

        :return: 格式化结果（如 "5.12MB"），无数据时返回 None
        """
        if self.total_comp is None:
            return None
        return PathFuc.utility.convert(self.total_comp)


# ============================== 核心门面类 ==============================

class ArchFuc(DefaultInstance):
    """
    压缩包管理工具门面类。

    封装双引擎（7-Zip / Bandizip）策略分发，提供统一的压缩、解压、测试与分析接口。
    """

    _EXCLUDED_FILE_TYPES: set[str] = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.android.package-archive",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/pdf",
    }  # 解压防误判白名单过滤（避免误解压）

    # ============================== 核心原语与进程调度 ==============================

    def __init__(
            self,
            engine: Literal["bz", "7z"] = "bz",
            encoding: str | None = None,
            auto_check: bool = True,
    ) -> None:
        """
        初始化压缩管理门面。

        :param engine: 引擎类别，可选 '7z' 或 'bz'（默认）
        :param encoding: CLI 管道输出解码编码（可选，默认自动匹配平台与引擎原生编码）
        :param auto_check: 是否在初始化时校验引擎可用性
        """
        engine_type = engine.lower()
        if engine_type not in {"bz", "7z"}:
            raise ValueError(f"Unsupported archive engine: {engine!r}. Expected 'bz' or '7z'")

        self._engine_type: Literal["bz", "7z"] = engine_type
        self._encoding = encoding

        self._engine: _BaseEngine = (
            _SevenZipEngine(self._encoding)
            if self._engine_type == "7z"
            else _BandizipEngine(self._encoding)
        )  # 实例化对应的引擎策略对象

        if auto_check and not self._engine.is_available():
            raise EnvironmentError(
                f"Archive engine '{self._engine_type}' is unavailable. Neither local binary nor system command was found"
            )

    # ============================== 分卷检测工具 ==============================

    @staticmethod
    def detect(path: str | Path) -> list[str]:
        """
        根据压缩包路径，检测并返回所有分卷或单文件压缩包路径列表。

        :param path: 压缩包路径
        :return: 排序后的分卷文件绝对路径列表
        """
        p = Path(path).expanduser()
        if not p.is_file():  # 主归档文件缺失时，尝试定位分卷首段
            candidates = [Path(f"{p}.001")]  # 7-Zip 数字分卷
            if p.suffix.lower() == ".zip":
                candidates.append(p.with_suffix(".z01"))  # ZIP 分卷
            if candidate := next((candidate for candidate in candidates if candidate.is_file()), None):
                p = candidate
            elif not p.exists():  # 输出尚不存在属于正常情况
                return []

        if not (p := check_path(p, target="file", error="warn")):
            return []

        p = p.resolve()  # 确保返回路径始终为绝对路径
        parent = p.parent  # 压缩包所在目录
        name = p.name.lower()

        def _natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
            """
            将文件名拆分为文本和数字片段，按数字大小自然排序。

            :param value: 待排序的文件名
            :return: 完成排序后的文件名
            """
            return tuple(
                (0, int(part)) if part.isdigit() else (1, part.casefold())
                for part in re.split(r"(\d+)", value)
            )

        patterns = [
            # RAR 分卷，首卷可能为 .partNN.rar 或 .exe（SFX）
            (r"^(.*?)(\.part\d+\.(rar|exe))$", lambda b: rf"^{re.escape(b)}\.part\d+\.(rar|exe)$"),
            # ZIP 分卷：.z01 ~ .z99 + .zip
            (r"^(.*?)(\.z\d{2}|\.zip)$", lambda b: rf"^{re.escape(b)}\.(z\d{{2}}|zip)$"),
            # 7Z 分卷：.7z.001 ~ .7z.999
            (r"^(.*?\.7z)\.\d{3}$", lambda b: rf"^{re.escape(b)}\.\d{{3}}$"),
            # 通用 001 分卷：xxx.001 / xxx.002 / ...
            (r"^(.*)\.\d{3}$", lambda b: rf"^{re.escape(b)}\.\d{{3}}$"),
        ]  # 定义常见分卷压缩包的正则匹配规则

        for pattern, builder in patterns:  # 遍历预定义的模式，匹配当前文件名；针对变量名为分卷名方式
            if m := re.match(pattern, name):
                base = m.group(1)  # 提取压缩包基名
                regex = builder(base)  # 在目录中查找所有匹配的分卷文件，并按名称排序返回
                matched = [
                    f for f in parent.iterdir()
                    if f.is_file() and re.match(regex, f.name, re.IGNORECASE)
                ]
                if matched:
                    matched.sort(key=lambda f: (_natural_key(f.name), f.name.casefold()))
                    return [str(f) for f in matched]

        return [str(p)]

    # ============================== 核心业务动作接口 ==============================

    @ClassProxy
    def analyze(self, archive: str | Path, password: str = "") -> ArchRes | bool:
        """
        获取压缩包详细信息。

        :param archive: 压缩包路径
        :param password: 解密密码
        :return: 成功返回 ArchRes，失败返回 False
        """
        if not (p := check_path(archive, target="file", error="warn")):
            return False
        return self._engine.analyze(p, password)

    @ClassProxy
    def compress(
            self,
            source: str | Path,
            output: str | Path = "",
            password: str = "",
            level: Literal[0, 1, 2, 3, 4, 5, 6, 7, 8, 9] = 9,
            include_root: bool = False,
            delete_source: bool = False,
            volume_size: str = "",
            exclude_types: Sequence[str] = (),
            archive_format: Literal["7z", "zip", "tar"] = "7z",
            overwrite_mode: Literal["s", "skip", "o", "overwrite", "u", "update"] = "skip",
    ) -> list[str] | bool:
        """
        压缩指定文件或目录。

        :param source: 待压缩文件或目录
        :param output: 压缩包目标路径（不含后缀）
        :param password: 压缩密码
        :param level: 压缩等级 0~9
        :param include_root: 是否包裹源目录本身
        :param delete_source: 成功后是否将源文件/目录移至回收站
        :param volume_size: 分卷大小（如 '100M'）
        :param exclude_types: 排除文件扩展名序列
        :param archive_format: 目标归档格式 ('7z', 'zip', 'tar', 'gz')
        :param overwrite_mode: 冲突策略 ('skip', 'overwrite', 'update')
        :return: 成功返回分卷文件列表，失败返回 False
        """
        # ------------------------------ 前置参数与路径校验 ------------------------------

        if not (src := check_path(source, target="any", error="warn")):
            return False

        mode_aliases = {
            "s": "s", "skip": "s",
            "o": "o", "overwrite": "o",
            "u": "u", "update": "u",
        }
        mode_name = str(overwrite_mode).lower()
        if mode_name not in mode_aliases:
            raise ValueError(f"Invalid compression overwrite mode: {overwrite_mode!r}")

        if not isinstance(level, int) or not (0 <= level <= 9):  # 校验压缩等级是否合法
            raise ValueError(f"Invalid compression level: {level!r}. Must be an integer between 0 and 9")

        base = str(src.parent / src.stem) if src.is_file() else str(src)  # 确定基准输出路径与最终归档文件路径
        out_base = str(output) if output else base
        ext = archive_format.lower()
        archive_path = Path(f"{out_base}.{ext}")

        # ------------------------------ 目标冲突策略处理 ------------------------------

        mode = mode_aliases[mode_name]
        if existing_archives := self.detect(archive_path):
            if mode in ("s", "skip"):  # 跳过压缩：直接检索并返回已存在的相关所有分卷文件
                return existing_archives
            elif mode in ("o", "overwrite"):  # 全新覆写：级联探测并清除同名的所有历史分卷文件，避免残留脏分卷
                for old_f in existing_archives:
                    if not PathFuc.utility.delete(old_f, explorer=False):
                        LogFuc.error(f"Failed to remove existing archive volume before overwrite: {old_f}")
                        return False

        # ------------------------------ 执行压缩与结果检验 ------------------------------

        success = self._engine.compress(
            source=src,
            output_base=out_base,
            password=password,
            level=level,
            include_root=include_root,
            delete_source=delete_source,
            volume_size=volume_size,
            exclude_types=exclude_types,
            archive_format=ext,
            overwrite_mode=mode,
        )  # 委派给具体引擎策略执行压缩

        if success:
            archives = self.detect(archive_path)
            if archives:
                if delete_source:
                    PathFuc.utility.delete(src, explorer=True)
                return archives

        return False

    @staticmethod
    def _commit_extract_result(
            source: Path,
            target: Path,
            overwrite_mode: str,
            backup_root: Path,
    ) -> bool:
        """
        将单个已成功解压的文件或目录提交到最终位置。

        :param source: 暂存文件或目录
        :param target: 最终目标路径
        :param overwrite_mode: 冲突策略 ('o'/'s'/'r')
        :param backup_root: 被覆盖目标的临时备份目录
        :return: 提交成功返回 True，失败时回滚并返回 False
        """
        target.parent.mkdir(parents=True, exist_ok=True)  # 确保最终目标的父目录存在
        target_exists = target.exists() or target.is_symlink()
        if target_exists and overwrite_mode == "s":
            return True  # 跳过冲突项，暂存内容随后由临时目录统一清理
        if target_exists and overwrite_mode == "r":
            index = 1
            while (candidate := target.with_name(
                    f"{target.stem} ({index}){target.suffix}")).exists() or candidate.is_symlink():
                index += 1
            target = candidate  # 重命名策略选择下一个可用路径

        backup = backup_root / "existing"
        try:
            if target_exists and overwrite_mode == "o":
                backup_root.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)  # 先暂存旧目标；提交失败时可原位恢复
            os.replace(source, target)  # 暂存与目标位于同一磁盘，整体移动避免逐文件写入
            return True
        except OSError as e:
            LogFuc.error(f"[extract_commit_error] Failed to commit extracted result: {e}")
            if backup.exists() or backup.is_symlink():
                try:
                    os.replace(backup, target)  # 提交失败时恢复被覆盖的旧文件或目录
                except OSError as rollback_error:
                    LogFuc.error(f"[extract_rollback_error] Failed to restore '{target}': {rollback_error}")
            return False

    @ClassProxy
    def extract(
            self,
            archive: str | Path,
            output: str | Path = "",
            password: str = "",
            extraction_mode: Literal["n", "name", "a", "auto"] = "auto",
            overwrite_mode: Literal["o", "overwrite", "s", "skip", "r", "rename"] = "rename",
            delete_archive: bool = False,
            force_extract: bool = False,
    ) -> bool:
        """
        解压压缩包，支持智能防炸包与容灾回滚。

        :param archive: 待解压文件路径
        :param output: 目标释放目录
        :param password: 解压密码
        :param extraction_mode: 解压排布模式 ('auto', 'name')
        :param overwrite_mode: 冲突策略 ('overwrite', 'skip', 'rename')
        :param delete_archive: 成功后是否将压缩包移入回收站
        :param force_extract: 是否跳过复合文档白名单过滤
        :return: 执行成功返回 True，失败返回 False
        """
        # ------------------------------ 前置校验与格式安全过滤 ------------------------------

        if not (arc := check_path(archive, target="file", error="warn")):
            return False

        extraction_modes = {"n": "name", "name": "name", "a": "auto", "auto": "auto"}
        mode = str(extraction_mode).lower()
        if mode not in extraction_modes:
            raise ValueError(f"Invalid extraction mode: {extraction_mode!r}")

        overwrite_modes = {
            "o": "o", "overwrite": "o",
            "s": "s", "skip": "s",
            "r": "r", "rename": "r",
        }
        overwrite_name = str(overwrite_mode).lower()
        if overwrite_name not in overwrite_modes:
            raise ValueError(f"Invalid extraction overwrite mode: {overwrite_mode!r}")
        overwrite_mode = overwrite_modes[overwrite_name]

        if not force_extract:  # 复合容器安全拦截：避免将 docx、xlsx、apk、pdf 等误当常规压缩包解散
            try:
                kind = filetype.guess(str(arc))
                if kind is not None and kind.mime in self._EXCLUDED_FILE_TYPES:
                    return False
            except (ImportError, OSError, ValueError):
                pass

        # ------------------------------ 暂存解压与最终目录策略解析 ------------------------------

        base_target = Path(output) if output else arc.parent
        base_target.parent.mkdir(parents=True, exist_ok=True)
        target = base_target / arc.stem if mode in ("n", "name") else base_target

        # ------------------------------ 暂存解压、提交与回滚 ------------------------------

        try:
            with tempfile.TemporaryDirectory(prefix="cdfuc-extract-", dir=base_target.parent) as temp_dir:
                temp_root = Path(temp_dir)  # 暂存目录与最终目标放在同一磁盘，失败时只需清理暂存区，不触碰用户目录
                staging = temp_root / "staging"
                staging.mkdir()

                success = self._engine.extract(
                    archive=arc,
                    target=staging,
                    password=password,
                    overwrite_mode="o",  # 暂存目录为空，不需要应用最终目标的冲突策略
                    delete_archive=False,
                )  # 委派给具体引擎策略执行解压
                if not success:
                    return False

                stage_entries = list(staging.iterdir())
                staged_result = staging
                if mode in ("a", "auto"):
                    if len(stage_entries) == 1:  # 单个顶层文件或目录直接保留原名放入输出目录
                        staged_result = stage_entries[0]
                        target = base_target / staged_result.name
                    else:
                        target = base_target / arc.stem  # 多个顶层项目（或空归档）统一套同名目录

                if not self._commit_extract_result(
                        source=staged_result,
                        target=target,
                        overwrite_mode=overwrite_mode,
                        backup_root=temp_root / "backup",
                ):
                    return False

                if delete_archive:
                    for f in self.detect(arc):
                        PathFuc.utility.delete(f, explorer=True)
                return True
        except OSError as e:
            LogFuc.error(f"Failed to stage or commit archive extraction: {e}")
            return False

    @ClassProxy
    def test(self, archive: str | Path, password: str = "") -> bool:
        """
        测试压缩包完整性及密码有效性。

        :param archive: 压缩包路径
        :param password: 解密密码
        :return: 测试通过返回 True，否则返回 False
        """
        if not (p := check_path(archive, target="file", error="warn")):
            return False
        return self._engine.test(p, password)


# ============================== 引擎抽象基类与具体实现策略 ==============================

class _BaseEngine(ABC):
    """
    压缩引擎底层抽象基类（策略模式）。

    规范双引擎的公共执行原语、编码转换、路径解析与接口契约。
    """

    def __init__(self, encoding: str | None = None) -> None:
        """
        初始化引擎策略。

        :param encoding: 可选的自定义文本解码字符集（为 None 时自动采用平台与引擎默认策略）
        """
        self._custom_encoding = encoding
        self._bin_path: str = self.resolve_bin()

    # ============================== 核心原语与进程调度 ==============================

    @abstractmethod
    def get_native_windows_encoding(self) -> str:
        """
        声明该引擎在 Windows 下平台原生的默认编码（7z 必须为 gbk，bz 必须为 utf-8）。
        """
        pass

    def _get_platform_encoding(self) -> str:
        """
        获取当前平台安全的控制台输出解码字符集。

        优先采用用户显式指定的自定义编码；若未指定，则在 Windows 下采用引擎原生编码，在 Linux/macOS 下自动降级采用系统文件系统编码。

        :return: 编码字符集名称字符串
        """
        if self._custom_encoding:
            return self._custom_encoding
        if sys.platform.startswith("win"):
            return self.get_native_windows_encoding()
        return sys.getfilesystemencoding()

    @staticmethod
    def _overwrite_flag(overwrite_mode: str) -> str:
        """
        将规范化后的覆写策略代码映射为归档引擎参数。

        :param overwrite_mode: 覆写策略代码
        :return: 对应的归档引擎参数
        """
        flags = {
            "o": "-aoa", "overwrite": "-aoa",
            "s": "-aos", "skip": "-aos",
            "r": "-aou", "rename": "-aou",
        }
        try:
            return flags[str(overwrite_mode).lower()]
        except KeyError:
            raise ValueError(f"Invalid extraction overwrite mode: {overwrite_mode!r}") from None

    def _run_blocking(self, cmd: Sequence[str]) -> subprocess.CompletedProcess:
        """
        同步阻塞执行器。

        专供 analyze、test 等瞬时返回且无进度流的命令，杜绝管道卡死。

        :param cmd: 可执行文件与参数序列
        :return: CompletedProcess 执行结果对象
        """
        return run(
            cmd,
            shell=False,
            encoding=self._get_platform_encoding(),
            errors="replace",
        )

    def _run_streaming(self, cmd: Sequence[str]) -> subprocess.CompletedProcess:
        """
        异步流式执行器。

        专供 compress、extract 等耗时、吐出进度流的操作，输出交由 LogFuc.stream 统一接管分发。

        :param cmd: 可执行文件与参数序列
        :return: CompletedProcess 执行结果对象
        """
        proc: subprocess.Popen = run(
            cmd,
            async_mode=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            encoding=self._get_platform_encoding(),
            errors="replace",
        )

        lines: list[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:  # 此时 line 已经是完全解码且拼接正确的 str 字符串
                line_clean = line.rstrip("\r\n")
                if line_clean:
                    lines.append(line_clean)
                    LogFuc.stream(line_clean, level="DEBUG")
            proc.stdout.close()

        return_code = proc.wait()
        return subprocess.CompletedProcess(
            args=list(cmd),
            returncode=return_code,
            stdout="\n".join(lines),
            stderr="",
        )

    # ============================== 引擎生命周期与环境校验 ==============================

    @classmethod
    @abstractmethod
    def resolve_bin(cls) -> str:
        """
        解析引擎可执行程序路径：优先匹配 cdfuc/bin 目录，回退至系统 PATH。

        :return: 可执行程序路径或系统命令名称
        """
        pass

    @abstractmethod
    def check_availability_code(self, return_code: int) -> bool:
        """
        校验引擎空运行时的退出码是否代表可用。

        :param return_code: 进程退出码
        :return: 是否可用
        """
        pass

    def is_available(self) -> bool:
        """
        检测当前引擎是否可用。

        :return: 可用返回 True，否则返回 False
        """
        clean_path = self._bin_path.strip('"')  # 去除外层安全引号，获取干净的路径或命令名
        if any(sep in clean_path for sep in (os.sep, "/", "\\")):  # 若包含路径特征（说明指向的是本地 bin 物理路径），校验其是否存在且为文件
            if not check_path(clean_path, target="file", error="ignore"):
                return False

        try:  # 仅捕获进程启动与系统调用层面的错误，避免吞掉未知代码异常
            res = run(clean_path, shell=False)
            return self.check_availability_code(res.returncode)
        except (OSError, ValueError) as e:
            LogFuc.error(f"[engine_check_error] {e}")
            return False

    # ============================== 抽象契约定义 ==============================

    def _run_analyze(
            self,
            args: list[str],
            archive: Path,
            password: str,
            *,
            password_prefix: str,
            engine_name: str,
    ) -> tuple[str, list[str], dict[str, int], dict[str, int]] | None:
        """
        执行归档信息读取命令，并创建统一的解析结果缓存。

        :param args: 引擎专属的基础命令参数
        :param archive: 待读取的压缩包路径
        :param password: 解密密码
        :param password_prefix: 引擎专属的密码参数前缀
        :param engine_name: 用于错误日志的引擎名称
        :return: 成功返回命令输出和空结果容器，失败返回 None
        """
        if password:
            args.append(f"{password_prefix}{password}")
        args.append(str(archive))

        res = self._run_blocking(args)
        if res.returncode != 0:
            LogFuc.error(f"{engine_name} analyze failed: {res.stdout}")
            return None

        return res.stdout or "", [], {}, {}

    @abstractmethod
    def analyze(self, archive: Path, password: str = "") -> ArchRes | bool:
        """
        解析压缩包元信息。
        """
        pass

    @abstractmethod
    def test(self, archive: Path, password: str = "") -> bool:
        """
        测试压缩包完整性。
        """
        pass

    @abstractmethod
    def compress(
            self,
            source: Path,
            output_base: str,
            password: str,
            level: int,
            include_root: bool,
            delete_source: bool,
            volume_size: str,
            exclude_types: Sequence[str],
            archive_format: str,
            overwrite_mode: str,
    ) -> list[str] | bool:
        """
        执行压缩。
        """
        pass

    @abstractmethod
    def extract(
            self,
            archive: Path,
            target: Path,
            password: str,
            overwrite_mode: str,
            delete_archive: bool,
    ) -> bool:
        """
        执行解压。
        """
        pass


class _SevenZipEngine(_BaseEngine):
    """
    7-Zip (7z.exe) 引擎策略实现。
    """

    # ============================== 引擎配置与可用性校验 ==============================

    def get_native_windows_encoding(self) -> str:
        """
        7-Zip 在 Windows 下默认使用 gbk（系统 OEM 代码页）。
        """
        return "gbk"

    @classmethod
    def resolve_bin(cls) -> str:
        """
        精准定位便携版 7z.exe，未命中时回退至系统 PATH。

        :return: 包裹双引号的绝对路径或系统命令 '7z'
        """
        bin_dir = Path(__file__).resolve().parent / "bin"  # 定位库内置二进制目录
        if bin_dir.is_dir():  # 递归检索匹配文件，并按路径层级深度排序（优先使用层级最浅的入口）
            if matched := sorted(bin_dir.glob("**/7z.exe"), key=lambda p: len(p.parts)):
                return str(matched[0])  # 参数列表会由 subprocess 正确处理路径中的空格
        return "7z"  # 未探测到便携二进制文件时，回退至系统 PATH 环境变量命令

    def check_availability_code(self, return_code: int) -> bool:
        """
        校验 7-Zip 可用性退出码（标准 7z.exe 正常退出码为 0）。

        :param return_code: 进程退出码
        :return: 正常返回 True
        """
        return return_code in {0, 7}  # 7 代表命令行参数错误

    # ============================== 归档审计与数据提取 ==============================

    def analyze(self, archive: Path, password: str = "") -> ArchRes | bool:
        """
        使用 7-Zip 技术清单模式 (-slt) 解析压缩包内所有文件的详细元数据。

        :param archive: 压缩包绝对路径
        :param password: 解密密码
        :return: 成功返回 ArchRes 实例，失败返回 False
        """
        result = self._run_analyze(
            [self._bin_path, "l", "-slt"],
            archive,
            password,
            password_prefix="-p",
            engine_name="7-Zip",
        )
        if result is None:
            return False
        output, files, orig_sizes, comp_sizes = result

        cur_file: str | None = None
        cur_size = cur_packed = 0
        is_folder = False

        for line in output.splitlines():
            line = line.strip()
            if not line:  # 空行表示一个条目区块结束，提交解析出的文件元数据
                if cur_file and not is_folder:
                    files.append(cur_file)
                    orig_sizes[cur_file], comp_sizes[cur_file] = cur_size, cur_packed
                cur_file, cur_size, cur_packed, is_folder = None, 0, 0, False
                continue

            if line.startswith("Path = "):  # 提取文件相对路径（过滤掉压缩包本身的路径描述）
                if (val := line[7:].strip()) not in (str(archive), archive.name):
                    cur_file = val
            elif line.startswith("Size = "):  # 提取解压后原始大小（字节）
                cur_size = int(val) if (val := line[7:].strip()).isdigit() else 0
            elif line.startswith("Packed Size = "):  # 提取压缩后占用大小（字节）
                cur_packed = int(val) if (val := line[14:].strip()).isdigit() else 0
            elif line.startswith("Folder = "):  # 标记是否为目录条目
                is_folder = (line[9:].strip() == "+")
            elif line.startswith("Attributes = "):  # 新版 7-Zip 在技术清单中使用 Attributes = D 表示目录
                is_folder = "D" in line[13:].split()

        if cur_file and not is_folder:  # 补丁处理：收尾最后一个没有被空行截断的文件条目
            files.append(cur_file)
            orig_sizes[cur_file], comp_sizes[cur_file] = cur_size, cur_packed

        LogFuc.debug(f"[7z] Successfully analyzed archive: {archive.name} ({len(files)} files found)")
        return ArchRes(package=archive.name, files=files, orig_sizes=orig_sizes, comp_sizes=comp_sizes)

    def test(self, archive: Path, password: str = "") -> bool:
        """
        使用 7-Zip 测试命令 (t) 校验压缩包完整性及密码正确性。

        :param archive: 压缩包路径
        :param password: 解密密码
        :return: 校验无误返回 True，否则返回 False
        """
        args = [self._bin_path, "t"]
        if password:
            args.append(f"-p{password}")
        args.append(str(archive))

        return self._run_streaming(args).returncode == 0

    # ============================== 压缩与解压执行层 ==============================

    def compress(
            self,
            source: Path,
            output_base: str,
            password: str,
            level: int,
            include_root: bool,
            delete_source: bool,
            volume_size: str,
            exclude_types: Sequence[str],
            archive_format: str,
            overwrite_mode: str,
    ) -> list[str] | bool:
        """
        组装 7-Zip 归档参数并执行流式压缩。

        :param source: 待压缩源路径
        :param output_base: 输出基准路径
        :param password: 压缩密码
        :param level: 压缩等级 (0~9)
        :param include_root: 是否包含根目录
        :param delete_source: 是否删除源文件
        :param volume_size: 分卷大小
        :param exclude_types: 排除文件后缀
        :param archive_format: 归档格式 ('7z', 'zip', 'tar', 'gz')
        :param overwrite_mode: 冲突模式
        :return: 成功返回 True，失败返回 False
        """
        ext = archive_format.lower()
        archive_path = Path(f"{output_base}.{ext}")

        args = [self._bin_path, "a", "-y", "-bsp1"]  # -bsp1 强制实时输出进度百分比流

        if ext in ("tar", "gz", "gzip"):  # tar 格式仅为打包归档容器，不支持配置通用压缩等级
            args.append(f"-t{ext}")
            if ext != "tar":
                args.append(f"-mx={level}")
        else:
            args.append(f"-t{ext}")
            args.append(f"-mx={level}")

        if password:  # 密码保护参数
            args.append(f"-p{password}")

        if volume_size:  # 分卷大小语法适配：7-Zip 为 -v100m
            args.append(f"-v{volume_size}")

        if exclude_types:  # 排除文件后缀语法适配
            clean_exts = [e.lstrip(".") for e in exclude_types if e.strip(".")]
            for e in clean_exts:
                args.append(f"-xr!*.{e}")

        target_src = str(source)  # 源目录根层级包裹处理：不包裹根目录时，7-Zip 需以通配符展开
        if not include_root and source.is_dir():
            target_src = str(source / "*")

        args.append(str(archive_path))
        args.append(target_src)

        res = self._run_streaming(args)
        return res.returncode == 0

    def extract(
            self,
            archive: Path,
            target: Path,
            password: str,
            overwrite_mode: str,
            delete_archive: bool,
    ) -> bool:
        """
        组装 7-Zip 释放参数并执行流式解压。

        :param archive: 压缩包路径
        :param target: 目标释放目录
        :param password: 解密密码
        :param overwrite_mode: 冲突策略 ('o'/'s'/'r')
        :param delete_archive: 是否删除源压缩包
        :return: 成功返回 True，失败返回 False
        """
        ao_opt = self._overwrite_flag(overwrite_mode)

        args = [self._bin_path, "x", "-y", ao_opt, "-bsp1"]  # -y: 禁用交互提示，全部默认 Yes
        if password:
            args.append(f"-p{password}")

        args.append(str(archive))
        args.append(f"-o{target}")

        return self._run_streaming(args).returncode == 0


class _BandizipEngine(_BaseEngine):
    """Bandizip (bz.exe) 引擎策略实现"""

    # ============================== 引擎配置与可用性校验 ==============================

    def get_native_windows_encoding(self) -> str:
        """
        Bandizip 在 Windows 下控制台默认强制使用 utf-8。
        """
        return "utf-8"

    @classmethod
    def resolve_bin(cls) -> str:
        """
        精准定位 Bandizip 控制台版本 bz.exe，未命中时回退至系统 PATH。

        :return: 包裹双引号的绝对路径或系统命令 'bz'
        """
        bin_dir = Path(__file__).resolve().parent / "bin"
        if bin_dir.is_dir():
            if matched := sorted(bin_dir.glob("**/bz.exe"), key=lambda p: len(p.parts)):
                return str(matched[0])
        return "bz"

    def check_availability_code(self, return_code: int) -> bool:
        """
        校验 Bandizip 可用性退出码（bz.exe 正常空运行返回码通常为 0 或 1）。

        :param return_code: 进程退出码
        :return: 合法则返回 True
        """
        return return_code in {0, 1}

    # ============================== 归档审计与数据提取 ==============================

    def analyze(self, archive: Path, password: str = "") -> ArchRes | bool:
        """
        使用 Bandizip 标准列表模式 (l) 解析压缩包内文件列表与大小。

        :param archive: 压缩包路径
        :param password: 解密密码
        :return: 成功返回 ArchRes 实例，失败返回 False
        """
        result = self._run_analyze(
            [self._bin_path, "l"],
            archive,
            password,
            password_prefix="-p:",
            engine_name="Bandizip",
        )
        if result is None:
            return False
        output, files, orig_sizes, comp_sizes = result
        table_regex = re.compile(
            r"^\s*[\d\-]+\s+[\d:]+\s+([A-Z_]{4})\s+(\d+)\s+(\d+)?\s+(.+?)\s*$"
        )  # 匹配列：日期 时间 属性 原始大小 [压缩大小] 文件名

        for line in output.splitlines():
            if m := table_regex.match(line):
                attr, orig, comp, fname = m.groups()
                fname = fname.strip()
                if attr.startswith("D") or fname.endswith(("/", "\\")):  # 跳过目录条目，仅保留文件
                    continue
                files.append(fname)
                orig_sizes[fname] = int(orig)
                comp_sizes[fname] = int(comp) if comp else int(orig)  # 若无压缩大小列则回退至原始大小

        LogFuc.debug(f"[bz] Successfully analyzed archive: {archive.name} ({len(files)} files found)")
        return ArchRes(package=archive.name, files=files, orig_sizes=orig_sizes, comp_sizes=comp_sizes)

    def test(self, archive: Path, password: str = "") -> bool:
        """
        使用 Bandizip 测试命令 (t) 校验压缩包完整性。

        :param archive: 压缩包路径
        :param password: 解密密码
        :return: 校验通过返回 True，否则返回 False
        """
        args = [self._bin_path, "t", "-y"]
        if password:
            args.append(f"-p:{password}")
        args.append(str(archive))
        return self._run_streaming(args).returncode == 0

    # ============================== 压缩与解压执行层 ==============================

    def compress(
            self,
            source: Path,
            output_base: str,
            password: str,
            level: int,
            include_root: bool,
            delete_source: bool,
            volume_size: str,
            exclude_types: Sequence[str],
            archive_format: str,
            overwrite_mode: str,
    ) -> list[str] | bool:
        """
        组装 Bandizip 归档参数并执行流式压缩。

        :param source: 待压缩源路径
        :param output_base: 输出基准路径
        :param password: 压缩密码
        :param level: 压缩等级
        :param include_root: 是否包含根目录
        :param delete_source: 是否删除源文件
        :param volume_size: 分卷大小
        :param exclude_types: 排除文件后缀
        :param archive_format: 归档格式
        :param overwrite_mode: 冲突模式
        :return: 成功返回 True，失败返回 False
        """
        ext = archive_format.lower()
        archive_path = Path(f"{output_base}.{ext}")

        args = [self._bin_path, "a", "-y", f"-l:{level}"]

        if password:
            args.append(f"-p:{password}")

        if volume_size:
            args.append(f"-v:{volume_size}")

        if exclude_types:
            clean_exts = [e.lstrip(".") for e in exclude_types if e.strip(".")]
            args.append(f'-ex:{";".join(f"*.{e}" for e in clean_exts)}')

        if not include_root and source.is_dir():
            args.append("-storeroot:no")

        args.append(str(archive_path))
        args.append(str(source))

        res = self._run_streaming(args)
        return res.returncode == 0

    def extract(
            self,
            archive: Path,
            target: Path,
            password: str,
            overwrite_mode: str,
            delete_archive: bool,
    ) -> bool:
        """
        组装 Bandizip 释放参数并执行流式解压。

        :param archive: 压缩包路径
        :param target: 目标目录
        :param password: 解密密码
        :param overwrite_mode: 冲突策略
        :param delete_archive: 是否删除源压缩包
        :return: 成功返回 True，失败返回 False
        """
        ao_opt = self._overwrite_flag(overwrite_mode)

        args = [self._bin_path, "x", "-y", ao_opt]

        if password:
            args.append(f"-p:{password}")

        args.append(str(archive))
        args.append(str(target))

        return self._run_streaming(args).returncode == 0
