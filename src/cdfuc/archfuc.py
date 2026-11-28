import os
import re
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, Literal, NamedTuple

from .basefuc import (
    ClassProxy, DefaultInstance, check_path, lazy_import,
)
from .charfuc import CharFuc
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

    _VOLUME_PATTERNS: tuple[tuple[str, str, Callable[[str], str]], ...] = (
        ("rar_part", r"^(.*?)(\.part\d+\.(rar|exe))$", lambda b: rf"^{re.escape(b)}\.part\d+\.(rar|exe)$"),
        ("zip", r"^(.*?)(\.z\d{2}|\.zip)$", lambda b: rf"^{re.escape(b)}\.(z\d{{2}}|zip)$"),
        ("7z", r"^(.*?\.7z)\.\d{3}$", lambda b: rf"^{re.escape(b)}\.\d{{3}}$"),
        ("rar_old", r"^(.*?)(\.rar|\.r\d{2})$", lambda b: rf"^{re.escape(b)}\.(rar|r\d{{2}})$"),
        ("numbered", r"^(.*)\.\d{3}$", lambda b: rf"^{re.escape(b)}\.\d{{3}}$"),
    )  # 覆盖 RAR、ZIP、7z 及通用数字分卷命名

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
    def _resolve_volume_anchor(path: str | Path) -> tuple[Path | None, bool]:
        """
        解析分卷检测的基准文件路径。

        :param path: 压缩包路径或分卷路径
        :return: (解析后的有效文件绝对路径，若目标不存在且无须继续扫描则为 None；是否可继续视为有效路径)
        """
        p = Path(path).expanduser()  # 展开用户目录符号（如 ~）
        if not p.is_file():  # 主文件不存在时，可能是分卷首卷或尚未生成的目标路径
            candidates = [Path(f"{p}.001")]  # 尝试匹配数字分卷首卷（如 archive.7z.001）
            if p.suffix.lower() == ".zip":
                candidates.append(p.with_suffix(".z01"))  # 尝试匹配 ZIP 分卷首卷

            matched = next((item for item in candidates if item.is_file()), None)
            if matched is not None:
                p = matched  # 切换到已存在的分卷首卷作为后续扫描锚点
            elif not p.exists():
                return None, True  # 路径尚不存在，视为合法空状态并跳过后续扫描

        if not (checked := check_path(p, target="file", error="ignore")):  # 排除目录或无效文件节点
            LogFuc.warning(f"Target path is not a valid file: {p}")
            return None, False

        return checked.resolve(), True  # 返回规范化后的绝对路径

    @staticmethod
    def _is_volume_set_complete(kind: str, matched_files: Sequence[Path]) -> bool:
        """
        检查已匹配的分卷组编号是否连续且包含必要卷。

        :param kind: 分卷格式标识（如 'zip', 'rar_old', 'rar_part', '7z', 'numbered'）
        :param matched_files: 已按自然顺序排序的分卷路径序列
        :return: 分卷组是否完整
        """
        names = [item.name.lower() for item in matched_files]  # 统一转小写，避免大小写混用影响后缀与序号提取

        if kind == "zip":
            has_final_zip = any(item.endswith(".zip") for item in names)  # 需包含 .zip 且前置分卷从 1 连续递增
            volume_numbers = [
                int(match.group(1))
                for item in names
                if (match := re.search(r"\.z(\d{2})$", item))
            ]  # 提取 .z01、.z02 等前置分卷序号
            return has_final_zip and volume_numbers == list(range(1, len(volume_numbers) + 1))

        if kind == "rar_old":
            has_first = any(item.endswith(".rar") for item in names)  # 老式 RAR 分卷通常以 .rar 作为首卷
            volume_numbers = [
                int(match.group(1))
                for item in names
                if (match := re.search(r"\.r(\d{2})$", item))
            ]  # 提取 .r00、.r01 等后续分卷序号
            return has_first and volume_numbers == list(range(len(volume_numbers)))

        number_pattern = r"\.part(\d+)\." if kind == "rar_part" else r"\.(\d{3})$"  # part 分卷提取中间序号，7z/通用数字分卷提取末尾三位序号
        volume_numbers = [
            int(match.group(1))
            for item in names
            if (match := re.search(number_pattern, item))
        ]  # 提取分卷序号并转为整数以便做连续性比对
        return volume_numbers == list(range(1, len(volume_numbers) + 1))  # 校验编号是否从 1 开始连续无跳号

    @classmethod
    def _scan_archive_volumes(cls, path: str | Path) -> tuple[list[Path], bool]:
        """
        查找压缩包分卷，并检查已发现的分卷编号是否连续且包含必要卷。

        :param path: 压缩包路径或分卷路径
        :return: (检测到的文件列表，按文件名可判断的分卷组是否完整)
        """
        anchor, valid = cls._resolve_volume_anchor(path)  # 解析基准文件并确认路径状态是否合法
        if not valid:
            return [], False  # 路径无效或不可用，直接返回失败状态
        if anchor is None:
            return [], True  # 目标路径尚不存在，视为合法空状态且无需扫描目录

        name = anchor.name.lower()  # 统一转小写以匹配分卷命名模式
        for kind, pattern, builder in cls._VOLUME_PATTERNS:
            if not (match := re.match(pattern, name)):
                continue  # 当前分卷规则不匹配锚点文件名，尝试下一条规则

            base = match.group(1)  # 提取归档基础前缀，用于构造同组全部分卷的匹配正则
            regex = builder(base)
            try:
                matched = [
                    item for item in anchor.parent.iterdir()
                    if item.is_file() and re.match(regex, item.name, re.IGNORECASE)
                ]  # 遍历锚点所在目录，收集同组所有分卷文件（忽略文件名大小写）
            except OSError as error:
                LogFuc.warning(f"Failed to read volume directory '{anchor.parent}': {error}")  # 目录无权限或不可访问时输出警告
                return [], False

            if not matched:
                continue  # 目录中未收集到匹配文件时继续尝试后续模式

            if kind == "rar_old":
                matched.sort(
                    key=lambda item: (
                        0 if item.suffix.lower() == ".rar" else int(item.suffix.lower()[2:]) + 1,
                        item.name.casefold(),
                    )
                )  # 老式 RAR 首卷为 .rar，后续卷依次为 .r00、.r01
            else:
                matched.sort(
                    key=lambda item: (CharFuc.process.sort_number(item.name), item.name.casefold())
                )  # 其他格式按自然顺序与大小写不敏感键排序
            return matched, cls._is_volume_set_complete(kind, matched)  # 返回排序后的分卷列表及完整性校验结果

        return [anchor], True  # 未命中任何多卷规则时，按普通单文件归档处理

    @classmethod
    def detect(cls, path: str | Path) -> list[str]:
        """
        根据压缩包路径检测完整的分卷组或单文件压缩包。

        :param path: 压缩包路径
        :return: 完整分卷组的绝对路径列表；分卷缺失或编号不连续时返回空列表
        """
        archives, complete = cls._scan_archive_volumes(path)
        if not complete:
            LogFuc.warning(f"Archive volume set is incomplete: {path}")
            return []
        return [str(item) for item in archives]

    # ============================== 归档解析与校验接口 ==============================

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
        try:
            return self._engine.analyze(p, password)  # 执行实际解析，捕获运行期可能出现的系统/文件 I/O 异常
        except OSError as error:
            LogFuc.error(f"Failed to analyze archive '{p}': {error}")  # 防止底层 I/O 或竞争条件导致的系统异常直接抛出
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
        try:
            return self._engine.test(p, password)
        except OSError as error:
            LogFuc.error(f"Failed to test archive '{p}': {error}")
            return False

    # ============================== 归档打包与释放接口 ==============================

    # ------------------------------ 参数与目标预处理 ------------------------------

    @staticmethod
    def _resolve_choice(value: str, aliases: dict[str, str], label: str) -> str:
        """
        将公开接口中的选项别名转换为内部使用的短代码。

        :param value: 用户传入的选项值
        :param aliases: 别名与内部代码的映射
        :param label: 参数名称，用于错误提示
        :return: 规范化后的内部代码
        """
        normalized = str(value).lower()  # 统一转小写，兼容大小写混写的别名输入
        if normalized not in aliases:
            raise ValueError(f"Invalid {label}: {value!r}")  # 非法选项直接抛出 ValueError，尽早暴露调用错误
        return aliases[normalized]

    @staticmethod
    def _recycle_paths(paths: Sequence[str | Path], success_context: str) -> None:
        """
        将操作成功后的源路径移入系统回收站。

        :param paths: 需要移入回收站的文件或目录路径
        :param success_context: 主操作成功状态，用于生成失败告警
        """
        for item in paths:
            if not PathFuc.utility.delete(item, explorer=True):  # 删源失败仅告警，不回滚已经成功的归档或解压结果
                LogFuc.warning(f"{success_context}, but path could not be moved to the recycle bin: {item}")

    def _is_blocked_container(self, archive: Path, force_extract: bool) -> bool:
        """
        检查归档是否属于默认禁止直接解压的复合文件类型。

        :param archive: 待检查的归档路径
        :param force_extract: 是否跳过复合文件类型拦截
        :return: 命中禁止类型且未强制解压时返回 True
        """
        if force_extract:
            return False  # 显式开启 force_extract 时放行复合容器
        try:
            kind = filetype.guess(str(archive))  # 基于文件头魔数识别真实类型，避免仅凭后缀误判
            return kind is not None and kind.mime in self._EXCLUDED_FILE_TYPES
        except (ImportError, OSError, ValueError):
            return False  # 类型探测异常时降级放行，交由后续归档引擎实际处理

    @staticmethod
    def _resolve_extract_result(base_target: Path, archive: Path, mode: str, staging: Path) -> tuple[Path, Path]:
        """
        根据解压模式和暂存目录内容确定待提交内容及最终目标。

        :param base_target: 用户指定的解压目标目录
        :param archive: 当前归档路径
        :param mode: 规范化后的解压模式（'n'/'a'）
        :param staging: 引擎解压后写入的暂存目录
        :return: (待提交的暂存路径，最终目标路径)
        """
        if mode == "n":
            return staging, base_target / archive.stem  # 'name' 模式始终将整个解压结果放入压缩包同名子目录

        entries = list(staging.iterdir())  # 仅读取一次顶层条目，避免重复遍历文件系统
        if len(entries) == 1:  # 'auto' 模式下单个顶层项目直接保留原名释放到目标目录
            entry = entries[0]
            return entry, base_target / entry.name
        return staging, base_target / archive.stem  # 多个顶层项目或空归档时套同名目录，防止散落文件污染目标目录

    def _normalize_compress_inputs(
            self,
            source: str | Path,
            output: str | Path,
            password: str,
            level: int,
            archive_format: str,
            overwrite_mode: str,
    ) -> tuple[Path, Path, str, str, str] | None:
        """
        校验并规范化压缩接口的路径、格式和冲突策略参数。

        :param source: 待压缩的文件或目录
        :param output: 输出归档基准路径
        :param password: 归档密码
        :param level: 压缩等级
        :param archive_format: 归档格式
        :param overwrite_mode: 目标冲突策略
        :return: (源路径，归档路径，输出前缀，格式，内部冲突策略)；校验未通过时返回 None
        """
        if not (src := check_path(source, target="any", error="warn")):
            return None  # 源路径不存在或不可访问时直接返回 None

        if not isinstance(archive_format, str) or archive_format.lower() not in {"7z", "zip", "tar"}:
            raise ValueError(f"Invalid archive format: {archive_format!r}. Expected '7z', 'zip' or 'tar'")

        mode = self._resolve_choice(
            overwrite_mode,
            {"s": "s", "skip": "s", "o": "o", "overwrite": "o", "u": "u", "update": "u"},
            "compression overwrite mode",
        )
        if type(level) is not int or not (0 <= level <= 9):  # bool 是 int 的子类，必须用 type(...) is int 排除 True/False
            raise ValueError(f"Invalid compression level: {level!r}. Must be an integer between 0 and 9")

        ext = archive_format.lower()
        if ext == "tar" and password:  # TAR 容器规范不支持加密，提前拦截避免引擎报错或误以为已加密
            LogFuc.error("TAR format does not support password encryption; choose 7z or ZIP instead")
            return None

        base = str(src.parent / src.stem) if src.is_file() else str(src)  # 文件默认以源文件名作为归档名，目录默认用目录路径
        output_base = str(output) if output else base
        archive_path = Path(f"{output_base}.{ext}")
        return src, archive_path, output_base, ext, mode

    def _prepare_compress_target(self, archive_path: Path, mode: str) -> tuple[list[Path], list[str] | None] | None:
        """
        检查已有归档并按冲突策略决定是否继续压缩。

        :param archive_path: 目标归档路径
        :param mode: 规范化后的冲突策略（'s'/'o'/'u'）
        :return: (已有归档分卷，跳过时的返回列表)；无法安全继续时返回 None
        """
        existing_archives, existing_complete = self._scan_archive_volumes(archive_path)
        if archive_path.exists() and not archive_path.is_file():  # 目标路径被目录等非文件节点占用时直接拒绝
            LogFuc.error(f"Archive output path exists but is not a file: {archive_path}")
            return None
        if not existing_complete and not existing_archives:  # 目录不可读或路径状态异常，无法安全评估冲突
            LogFuc.error(f"Could not safely inspect existing archive output: {archive_path}")
            return None
        if existing_archives and not existing_complete:  # 检测到残缺分卷组时，仅允许在覆盖模式下替换
            if mode != "o":
                LogFuc.error(f"Cannot use an incomplete archive volume set: {archive_path}")
                return None
            LogFuc.warning(f"Incomplete archive volumes will be replaced after compression succeeds: {archive_path}")
        elif existing_archives and mode == "s":  # 跳过模式：归档已存在则直接返回现有分卷列表
            return existing_archives, [str(item) for item in existing_archives]
        elif existing_archives and mode == "u" and len(existing_archives) > 1:  # 多卷归档不支持增量更新
            LogFuc.error("Updating split archives is not supported; use overwrite mode to rebuild the archive")
            return None

        return existing_archives, None  # 返回已有归档供后续更新或提交时替换，None 表示不走跳过分支

    @staticmethod
    def _map_staged_archives(staged_archives: Sequence[Path], staged_base: Path, output_base: str) -> list[Path] | None:
        """
        将暂存目录内的归档分卷映射到用户指定的目标路径。

        :param staged_archives: 已生成的暂存归档文件
        :param staged_base: 暂存归档的文件名前缀
        :param output_base: 最终归档的文件名前缀
        :return: 对应的最终归档路径列表；发现非预期文件名时返回 None
        """
        targets: list[Path] = []
        for staged in staged_archives:
            if not staged.name.startswith(staged_base.name):  # 防止暂存区混入非预期命名的文件
                LogFuc.error(f"Unexpected staged archive name: {staged}")
                return None
            suffix = staged.name[len(staged_base.name):]  # 保留分卷后缀（如 .7z、.7z.001、.z01 + .zip）
            targets.append(Path(f"{output_base}{suffix}"))
        return targets

    # ------------------------------ 暂存事务与提交回滚 ------------------------------

    @staticmethod
    def _commit_files(
            staged_files: Sequence[Path],
            target_files: Sequence[Path],
            remove_targets: Sequence[Path],
            backup_root: Path,
    ) -> bool:
        """
        将暂存文件提交到目标路径，并在提交失败时恢复旧文件。

        :param staged_files: 已生成的暂存文件列表
        :param target_files: 暂存文件对应的最终路径列表
        :param remove_targets: 成功提交后应移除的旧文件（包括过期分卷）
        :param backup_root: 与目标文件位于同一磁盘的备份目录
        :return: 全部提交成功返回 True；失败时尝试回滚并返回 False
        """
        if len(staged_files) != len(target_files):  # 暂存文件与目标路径必须一一对应，数量不一致说明调用或映射异常
            LogFuc.error("[file_commit_error] Staged files and target paths do not match")
            if backup_root.exists() and not any(backup_root.iterdir()):  # 仅在备份目录存在且为空时清理，避免误删已有内容
                if not PathFuc.utility.delete(backup_root, explorer=False):  # 移除提前创建的空备份目录，减少失败后的目录残留
                    LogFuc.warning(f"[file_commit_backup] Could not remove empty backup directory: {backup_root}")
            return False

        backups: list[tuple[Path, Path]] = []
        committed: list[Path] = []
        affected_targets = list(dict.fromkeys([*target_files, *remove_targets]))  # 去重并保留顺序，同时覆盖过期分卷

        try:
            for target in affected_targets:
                target.parent.mkdir(parents=True, exist_ok=True)  # 确保所有目标父目录已就绪

            for target in affected_targets:
                if target.exists() or target.is_symlink():
                    backup = backup_root / f"{len(backups)}-{target.name}"  # 编号前缀避免同名冲突
                    backup_root.mkdir(parents=True, exist_ok=True)
                    if not PathFuc.utility.move(
                            target, backup, smart=False, explorer=False, conflict="warn",
                    ):  # 同盘移动旧文件到备份区，便于失败时原位恢复
                        raise OSError(f"Could not back up existing target: {target}")
                    backups.append((backup, target))

            for source, target in zip(staged_files, target_files):
                if not PathFuc.utility.move(
                        source, target, smart=False, explorer=False, conflict="warn",
                ):  # 将暂存文件原子/整体替换到最终目标位置
                    raise OSError(f"Could not move staged file to target: {target}")
                committed.append(target)

            if backup_root.exists():
                if not PathFuc.utility.delete(backup_root, explorer=False):  # 全部提交成功后清理旧文件备份
                    LogFuc.warning(f"[file_commit_backup] New files committed, but old backup remains: {backup_root}")
            return True

        except OSError as error:
            LogFuc.error(f"[file_commit_error] Failed to commit staged files: {error}")
            rollback_failed = False

            for target in reversed(committed):  # 逆序清理已写入的新目标，避免残留半成品
                if (target.exists() or target.is_symlink()) and not PathFuc.utility.delete(target, explorer=False):
                    rollback_failed = True
                    LogFuc.error(f"[file_commit_rollback] Failed to remove new target '{target}'")

            for backup, target in reversed(backups):  # 逆序恢复此前移走的旧目标
                if not PathFuc.utility.move(
                        backup, target, smart=False, explorer=False, conflict="warn",
                ):
                    rollback_failed = True
                    LogFuc.error(f"[file_commit_rollback] Failed to restore '{target}' from '{backup}'")

            if backup_root.exists():
                if rollback_failed:  # 回滚不完整时保留备份供人工恢复
                    LogFuc.error(f"[file_commit_rollback] Recovery files retained at: {backup_root}")
                elif not PathFuc.utility.delete(backup_root, explorer=False):  # 回滚完成后清理备份目录
                    LogFuc.warning(f"[file_commit_backup] Could not remove empty backup directory: {backup_root}")
            return False

    def _run_transaction(
            self,
            parent: Path,
            prefix: str,
            operation: Callable[[Path], tuple[Sequence[Path], Sequence[Path], Sequence[Path]] | None],
    ) -> bool:
        """
        在目标同盘创建暂存目录和备份目录，执行操作并统一提交结果。

        :param parent: 最终目标所在目录
        :param prefix: 临时目录名前缀
        :param operation: 接收暂存目录，成功时返回(暂存文件，目标文件，需移除的旧文件)
        :return: 操作及提交均成功时返回 True
        """
        backup_root: Path | None = None
        try:
            parent.mkdir(parents=True, exist_ok=True)
            backup_root = Path(
                tempfile.mkdtemp(prefix=f".{prefix}-backup-", dir=parent)
            )  # 独立于 TemporaryDirectory，防止回滚异常时连带丢失备份
            with tempfile.TemporaryDirectory(prefix=f"{prefix}-", dir=parent) as temp_dir:  # 与目标同盘，保证可高效移动
                staging = Path(temp_dir) / "staging"
                staging.mkdir()
                result = operation(staging)
                if result is None:
                    return False
                staged_files, target_files, remove_targets = result
                return self._commit_files(staged_files, target_files, remove_targets, backup_root)

        except OSError as error:
            LogFuc.error(f"Failed to stage or commit {prefix}: {error}")
            return False
        finally:
            if backup_root is not None and backup_root.exists():  # 若未用到 backup_root（如提前失败或跳过），顺手清理空目录
                try:
                    if not any(backup_root.iterdir()):
                        if not PathFuc.utility.delete(backup_root, explorer=False):
                            LogFuc.warning(f"Could not remove empty backup directory '{backup_root}'")
                except OSError as cleanup_error:
                    LogFuc.warning(f"Could not remove empty backup directory '{backup_root}': {cleanup_error}")

    def _prepare_compress_transaction(
            self,
            staging: Path,
            source: Path,
            archive_path: Path,
            output_base: str,
            archive_format: str,
            password: str,
            level: int,
            include_root: bool,
            delete_source: bool,
            volume_size: str,
            exclude_types: Sequence[str],
            mode: str,
            existing_archives: Sequence[Path],
    ) -> tuple[Sequence[Path], Sequence[Path], Sequence[Path]] | None:
        """
        调用压缩引擎并生成归档提交清单。

        :param staging: 当前事务的暂存目录
        :param source: 待压缩的文件或目录
        :param archive_path: 最终归档路径
        :param output_base: 最终归档文件名前缀
        :param archive_format: 归档格式
        :param password: 归档密码
        :param level: 压缩等级
        :param include_root: 是否保留源目录根节点
        :param delete_source: 是否在压缩成功后删除源路径
        :param volume_size: 分卷大小
        :param exclude_types: 排除的文件扩展名
        :param mode: 规范化后的冲突策略
        :param existing_archives: 已存在的归档分卷
        :return: (暂存分卷，最终分卷，需替换的旧分卷)；压缩或校验失败时返回 None
        """
        temp_base = staging / "archive"
        staged_archive = Path(f"{temp_base}.{archive_format}")

        if mode == "u" and existing_archives:  # 单卷更新先复制旧归档，在副本上执行更新；失败时不会损坏原归档
            if not PathFuc.utility.copy(
                    existing_archives[0], staged_archive, smart=False, explorer=False, conflict="warn",
            ):
                raise OSError(f"Could not copy existing archive for update: {existing_archives[0]}")

        if not self._engine.compress(
                source=source,
                output_base=str(temp_base),
                password=password,
                level=level,
                include_root=include_root,
                delete_source=delete_source,
                volume_size=volume_size,
                exclude_types=exclude_types,
                archive_format=archive_format,
                overwrite_mode=mode,
        ):
            return None

        staged_archives, complete = self._scan_archive_volumes(staged_archive)  # 校验暂存区生成的单卷或多卷是否完整
        if not staged_archives or not complete:
            LogFuc.error(f"Compression did not produce a complete archive: {archive_path}")
            return None

        targets = self._map_staged_archives(staged_archives, temp_base, output_base)
        if targets is None:
            return None
        return staged_archives, targets, existing_archives  # existing_archives 作为 remove_targets，清理旧单卷或历史多余分卷

    def _prepare_extract_transaction(
            self,
            staging: Path,
            archive: Path,
            base_target: Path,
            password: str,
            extraction_mode: str,
            overwrite_mode: str,
    ) -> tuple[Sequence[Path], Sequence[Path], Sequence[Path]] | None:
        """
        调用解压引擎并根据解压布局生成最终提交清单。

        :param staging: 当前事务的暂存目录
        :param archive: 待解压归档路径
        :param base_target: 用户指定的解压目标目录
        :param password: 解压密码
        :param extraction_mode: 规范化后的解压模式
        :param overwrite_mode: 规范化后的冲突策略
        :return: (暂存内容，最终目标，需移除的旧路径)；解压失败时返回 None
        """
        if not self._engine.extract(
                archive=archive,
                target=staging,
                password=password,
                overwrite_mode="o",  # 暂存目录为空，不需要应用最终目标的冲突策略
                delete_archive=False,  # 推迟到事务提交成功后再删归档，避免解压/提交失败导致原包丢失
        ):
            return None

        staged_result, target = self._resolve_extract_result(
            base_target=base_target,
            archive=archive,
            mode=extraction_mode,
            staging=staging,
        )
        target_exists = target.exists() or target.is_symlink()
        if target_exists and overwrite_mode == "s":
            return (), (), ()  # 跳过冲突目标，不提交暂存内容
        if target_exists and overwrite_mode == "r":
            index = 1
            while ((candidate := target.with_name(f"{target.stem} ({index}){target.suffix}")).exists()
                   or candidate.is_symlink()):
                index += 1
            target = candidate

        return (staged_result,), (target,), ()

    # ------------------------------ 归档打包与释放接口 ------------------------------

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
        :param archive_format: 目标归档格式 ('7z', 'zip', 'tar')
        :param overwrite_mode: 冲突策略；Bandizip 不支持更新已有 TAR，需使用覆盖模式重建
        :return: 成功返回分卷文件列表，失败返回 False
        """
        # ------------------------------ 前置参数与路径校验 ------------------------------

        normalized = self._normalize_compress_inputs(
            source=source,
            output=output,
            password=password,
            level=level,
            archive_format=archive_format,
            overwrite_mode=overwrite_mode,
        )  # 校验源路径、压缩等级、冲突策略及 tar 加密限制，并解析目标路径
        if normalized is None:
            return False
        src, archive_path, output_base, ext, mode = normalized

        # ------------------------------ 目标冲突策略处理 ------------------------------

        prepared = self._prepare_compress_target(archive_path, mode)  # 检查已有归档/分卷状态并应用 skip/overwrite/update 规则
        if prepared is None:
            return False
        existing_archives, skipped_result = prepared
        if skipped_result is not None:  # 命中 skip 且目标已存在时直接返回已有归档路径
            return skipped_result

        # ------------------------------ 执行压缩与结果检验 ------------------------------

        try:
            if not self._run_transaction(
                    parent=archive_path.parent,
                    prefix="cdfuc-compress",
                    operation=lambda staging: self._prepare_compress_transaction(
                        staging=staging,
                        source=src,
                        archive_path=archive_path,
                        output_base=output_base,
                        archive_format=ext,
                        password=password,
                        level=level,
                        include_root=include_root,
                        delete_source=delete_source,
                        volume_size=volume_size,
                        exclude_types=exclude_types,
                        mode=mode,
                        existing_archives=existing_archives,
                    ),
            ):  # 在同盘暂存区执行压缩并事务式提交/回滚
                return False

            archives = self.detect(archive_path)  # 提交完成后再次探测最终归档组，确保返回结果完整有效
            if not archives:
                return False
            if delete_source:  # 仅在归档生成并校验通过后再将源路径移入回收站
                self._recycle_paths([src], "Archive created")
            return archives
        except OSError as error:  # 兜底捕获暂存、提交或后续检测阶段的系统/文件 I/O 异常
            LogFuc.error(f"Failed to stage or commit archive compression: {error}")
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

        if not (arc := check_path(archive, target="file", error="warn")):  # 校验待解压文件是否存在且为有效文件
            return False

        extraction_mode = self._resolve_choice(
            extraction_mode,
            {"n": "n", "name": "n", "a": "a", "auto": "a"},
            "extraction mode",
        )  # 规范化解压排布模式（n/name -> 'n'，a/auto -> 'a'）

        overwrite_mode = self._resolve_choice(  # 规范化解压冲突策略（o/overwrite -> 'o'，s/skip -> 's'，r/rename -> 'r'）
            overwrite_mode,
            {"o": "o", "overwrite": "o", "s": "s", "skip": "s", "r": "r", "rename": "r"},
            "extraction overwrite mode",
        )
        if self._is_blocked_container(arc, force_extract):  # 默认拦截 docx/xlsx/pdf/apk 等复合文档误解压
            return False

        # ------------------------------ 暂存解压与最终目录策略解析 ------------------------------

        base_target = Path(output) if output else arc.parent  # 未指定输出目录时默认释放到压缩包所在目录

        # ------------------------------ 暂存解压、提交与回滚 ------------------------------

        success = self._run_transaction(  # 在同盘暂存区解压、解析目标路径并事务式提交/回滚
            parent=base_target.parent,
            prefix="cdfuc-extract",
            operation=lambda staging: self._prepare_extract_transaction(
                staging=staging,
                archive=arc,
                base_target=base_target,
                password=password,
                extraction_mode=extraction_mode,
                overwrite_mode=overwrite_mode,
            ),
        )
        if success and delete_archive:  # 仅在完整解压并成功提交后才将源归档（含多卷）移入回收站
            self._recycle_paths(self.detect(arc), "Extraction completed")
        return success


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

        if ext == "zip" and password and not password.isascii():
            LogFuc.error(
                "7-Zip cannot reliably encode non-ASCII ZIP passwords; use an ASCII password, Bandizip, or 7z format"
            )
            return False

        command = "u" if overwrite_mode == "u" and archive_path.is_file() else "a"  # 新建归档使用 a；更新已有归档使用 u，避免把更新策略退化为普通添加
        args = [self._bin_path, command, "-y", "-bsp1"]  # -bsp1 强制实时输出进度百分比流

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

        if ext == "tar" and overwrite_mode == "u" and archive_path.is_file():
            LogFuc.error("Bandizip cannot update an existing TAR archive; use overwrite mode to rebuild it")
            return False

        overwrite = "c" if overwrite_mode == "o" else "a"  # c 重建同名归档；a 添加文件或创建新归档
        args = [self._bin_path, overwrite, "-y", f"-fmt:{ext}"]
        if ext != "tar":  # TAR 仅负责打包，不使用压缩等级参数
            args.append(f"-l:{level}")

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
