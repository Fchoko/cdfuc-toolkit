import re
from typing import Literal, NamedTuple

import unicodedata

from .basefuc import DefaultInstance, DelegatedBase, lazy_import, module
from .timefuc import TimeFuc


# ============================== 结果数据结构 ==============================

class ConvertRes(NamedTuple):
    """
    中文/日文数字转换结果数据结构。
    """

    text: str  # 转换后的完整字符串
    originals: list[str]  # 原始数字片段列表（转换前）
    mappings: dict[str, str]  # 原始数字片段到阿拉伯数字的映射字典


class BracketRes(NamedTuple):
    """
    括号内容提取结果数据结构。
    """

    round: list[str]  # 小括号 () 中匹配到的内容
    square: list[str]  # 中括号 [] 中匹配到的内容
    curly: list[str]  # 大括号 {} 中匹配到的内容
    ordered: list[str]  # 按原字符串顺序排列


# ============================== 核心门面与子模块 ==============================

class CharFuc(DefaultInstance):
    """
    字符处理工具类。

    子模块：
        detect    → 字符识别
        process   → 字符处理
        clipboard → 剪贴板工具
    """

    detect: "_Detect"  # 此处增强 IDE 静态提示
    process: "_Process"
    clipboard: "_Clipboard"

    detect = module(lambda parent: _Detect(parent))  # 模块代理，默认实例名为子类名小写
    process = module(lambda parent: _Process(parent))
    clipboard = module(lambda parent: _Clipboard(parent))

    def __init__(self, encoding: str = "utf-8") -> None:
        """
        初始化工具实例。

        :param encoding: 默认字符串编码
        """
        self._encoding = encoding


class _Detect(DelegatedBase[CharFuc]):
    """字符串识别工具"""

    _IGNORED_CHARS = {
        "々", "〆", "θ", "ｶ", "ﾘ", "ﾊ", "ﾟ", "ー"
    }  # 检测语言时忽略的特殊字符

    _LANGUAGE_RANGES = {
        "en": lambda c: ord(c) < 128,  # 英文（ASCII 范围内）
        "ru": lambda c: 0x0400 <= ord(c) <= 0x04FF,  # 俄文（西里尔字母）
        "zh": lambda c: 0x4E00 <= ord(c) <= 0x9FFF,  # 中文（常用汉字范围）
        "ja": lambda c: 0x3040 <= ord(c) <= 0x30FF or 0x4E00 <= ord(c) <= 0x9FFF,  # 日文（假名及通用汉字）
        "ko": lambda c: 0xAC00 <= ord(c) <= 0xD7AF,  # 韩文（现代朝鲜语音节块）
    }  # 语言字符范围

    @staticmethod
    def is_number(value: int | float | str | None | bool) -> bool:
        """
        判断对象是否可视为数字。

        :param value: 待检测对象
        :return: 是否为数字
        """
        if value is None or isinstance(value, bool):  # bool 是 int 的子类，必须优先排除
            return False

        if isinstance(value, (int, float)):  # 数值类型直接返回 True
            return True

        if isinstance(value, str):  # 字符串尝试解析
            try:
                float(value.strip())
                return True
            except ValueError:
                return False

        return False

    def match_language(self, text: str, lang: Literal["en", "ru", "zh", "ja", "ko"] = "en") -> bool:
        """
        判断字符串是否仅由指定语言字符组成。

        会自动忽略：数字、标点符号、空格和 _IGNORED_CHARS 中定义的特殊字符。

        :param text: 待检测字符串
        :param lang: 语言代码（en-英文 / ru-俄文 / zh-中文 / ja-日文 / ko-韩文）
        :return: True：全部属于指定语言；False：存在其他语言字符
        :raises ValueError: 当传入不受支持的语言代码时抛出
        """
        target_lang = lang.lower()
        if target_lang not in self._LANGUAGE_RANGES:
            raise ValueError(f"Unsupported language: {lang!r}")

        chars = [
            ch for ch in text
            if ch.isalpha() and ch not in self._IGNORED_CHARS
        ]  # 过滤非字母字符及特殊排除字符

        if not chars:  # 没有有效字符时返回 False
            return False

        validator = self._LANGUAGE_RANGES[target_lang]
        return all(validator(ch) for ch in chars)


class _Process(DelegatedBase[CharFuc]):
    """字符串处理工具"""

    _CN_NUMBER_CHARS = set("零一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟")  # 中文数字字符

    _JP_NUMBER_MAP = {
        "壱": "一",
        "弐": "二",
        "参": "三",
        "陸": "六",
        "漆": "七",
    }  # 日文大写数字映射

    _ROMAJI_NUMBER_MAP = {
        "hachi": "八",
        "kyuu": "九",
        "roku": "六",
        "nana": "七",
        "ichi": "一",
        "juu": "十",
        "san": "三",
        "yon": "四",
        "shi": "四",
        "ni": "二",
        "go": "五",
        "ku": "九",
    }  # 日文罗马音映射表（降序排列优先匹配长词素）

    _ROMAJI_PATTERN = re.compile(
        r"\b(" + "|".join(_ROMAJI_NUMBER_MAP.keys()) + r")\b",
        flags=re.IGNORECASE,
    )  # 罗马音独立单词正则（使用单词边界 \b 防止误伤包含该子串的英文单词）

    _BRACKET_PATTERNS = {
        "round": re.compile(r"\((.*?)\)"),
        "square": re.compile(r"\[(.*?)]"),
        "curly": re.compile(r"\{(.*?)}"),
    }  # 括号匹配预编译规则

    @classmethod
    def _convert_number(cls, text: str) -> str:
        """
        将连续中文/日文大写数字片段转换为阿拉伯数字字符串。

        :param text: 数字文本片段
        :return: 转换后的阿拉伯数字文本
        :raises ImportError: 当未安装第三方库 cn2an 时抛出
        """
        cn2an = lazy_import("cn2an")
        normalized = "".join(cls._JP_NUMBER_MAP.get(ch, ch) for ch in text)  # 日文大写数字归一化

        if normalized.isdigit():  # 纯数字直接返回
            return normalized

        try:
            return str(cn2an.cn2an(normalized))
        except (ValueError, TypeError):
            return text  # 无法解析时安全回退返回原样

    def base64_convert(self, text: str, encode: bool = True) -> str:
        """
        对字符串执行 Base64 编码或解码操作。

        :param text: 输入文本
        :param encode: True=编码，False=解码
        :return: 结果字符串
        """
        import base64

        try:
            encoding = getattr(self._parent, "_encoding", "utf-8")
            raw_bytes = text.encode(encoding)  # 将输入字符串编码为字节

            return (
                base64.b64encode(raw_bytes).decode(encoding)
                if encode
                else base64.b64decode(raw_bytes).decode(encoding)
            )

        except Exception as e:
            raise ValueError(f"Base64 convert failed: {e}") from None

    @staticmethod
    def collect_inputs(end: str = "end", reset: str = "reset", strip: bool = True) -> list[str]:
        """
        交互式收集多行输入。

        :param end: 结束标记
        :param reset: 重置标记
        :param strip: 是否去除首尾空格
        :return: 输入结果列表
        """
        inputs: list[str] = []
        print(f"Input (end='{end}', reset='{reset}'):")

        while True:
            try:
                raw = input()
            except EOFError:
                break

            text = raw.strip() if strip else raw

            if text.lower() == end.lower():
                break

            if text.lower() == reset.lower():
                inputs.clear()
                print(">>>> Reset! Please start entering again:")
                continue

            inputs.append(text)

        return inputs

    @classmethod
    def convert_numbers(cls, text: str) -> ConvertRes:
        """
        将字符串中的中、日文数字（包含独立罗马音）转换为阿拉伯数字。

        :param text: 输入文本
        :return: 如 ConvertRes(text='第11话', originals=['十一'])
        """

        def _replace_romaji(match: re.Match[str]) -> str:
            """
            将独立罗马音转换为中文数字字符，并记录其原始片段。

            :param match: 罗马音匹配对象
            :return: 对应的中文数字字符
            """
            word = match.group(0)
            convert = cls._ROMAJI_NUMBER_MAP[word.lower()]

            romaji_sources[match.start()] = word  # 替换结果目前均为一个字符，因此可记录其在替换后文本中的位置
            return convert

        def _flush_number() -> None:
            """
            转换并记录当前累积的数字片段。

            number_buffer 保存待转换内容，original_buffer 保存其原始文本。
            """
            if not number_buffer:
                return

            converted = cls._convert_number(number_buffer)
            originals.append(original_buffer)
            mappings[original_buffer] = converted
            parts.append(converted)

        romaji_sources: dict[int, str] = {}  # 记录替换后字符位置对应的罗马音原文
        processed_text = cls._ROMAJI_PATTERN.sub(_replace_romaji, text)  # 预先将独立罗马音规整为中文字符

        parts: list[str] = []
        originals: list[str] = []
        mappings: dict[str, str] = {}
        number_buffer = ""
        original_buffer = ""

        for index, ch in enumerate(processed_text):
            if (
                    ch.isdigit()
                    or ch in cls._CN_NUMBER_CHARS
                    or ch in cls._JP_NUMBER_MAP
            ):  # 如果字符是数字（中文或日文），则累积
                number_buffer += ch
                original_buffer += romaji_sources.get(index, ch)
                continue

            if number_buffer:  # 遇到非数字字符，先转换并记录已累积的数字
                _flush_number()
                number_buffer = ""
                original_buffer = ""

            parts.append(ch)  # 将非数字字符直接添加到结果中

        if number_buffer:  # 处理字符串末尾的数字片段
            _flush_number()

        return ConvertRes(
            text="".join(parts),
            originals=originals,
            mappings=mappings,
        )

    @classmethod
    def extract_brackets(cls, text: str, fullwidth: bool = False) -> BracketRes:
        """
        提取字符串中各种括号内的内容（小括号、中括号、大括号）。

        :param text: 输入字符串
        :param fullwidth: 是否将全角括号转换为半角括号
        :return: BracketRes（小括号内容, 中括号内容, 大括号内容）
        """
        target_text = text
        if fullwidth:  # 替换全角括号为半角括号
            target_text = target_text.translate(str.maketrans("（）【】｛｝", "()[]{}"))

        patterns = {
            "round": cls._BRACKET_PATTERNS["round"],
            "square": cls._BRACKET_PATTERNS["square"],
            "curly": cls._BRACKET_PATTERNS["curly"],
        }
        matches = {
            name: list(pattern.finditer(target_text))
            for name, pattern in patterns.items()
        }

        ordered = sorted(
            (
                (match.start(), match.group(1))
                for group in matches.values()
                for match in group
            ),
            key=lambda item: item[0],
        )

        return BracketRes(
            round=[match.group(1) for match in matches["round"]],
            square=[match.group(1) for match in matches["square"]],
            curly=[match.group(1) for match in matches["curly"]],
            ordered=[content for _, content in ordered],
        )

    @staticmethod
    def simplify_text(text: str, protected: bool = False) -> str:
        """
        生成用于宽松匹配的最简字符串。

        仅用于比较，不会修改原始字符串；处理规则：
            - 去除所有空白字符；
            - 去除所有标点符号；
            - 转换为小写。

        :param text: 待比较的字符串
        :param protected: 是否保护正则表达式语法
        :return: 用于最简式匹配的字符串
        """
        if protected:
            return "".join(ch.lower() for ch in text if not ch.isspace())

        return "".join(
            ch.lower()  # 忽略大小写
            for ch in text
            if not ch.isspace()  # 去除空白字符
            and not unicodedata.category(ch).startswith("P")  # P 表示 Unicode 标点符号类别
        )

    @staticmethod
    def sort_language(items: list[str]) -> list[str]:
        """
        按语言类型排序：英文/数字/符号排前面，其他语言（如中文、日文）排后面。

        :param items: 字符串列表
        :return: 排序结果
        """
        pattern = re.compile(r"^[a-zA-Z0-9\s\W]+$")  # 匹配全英文、数字与基本符号
        return sorted(items, key=lambda x: (not pattern.match(x), x))

    @staticmethod
    def sort_number(value: str) -> tuple[tuple[int, int | str], ...]:
        """
        按文件名中的数字片段生成自然排序键。

        :param value: 待排序的文件名字符串
        :return: 数字与大小写不敏感文本混合的排序元组
        """
        return tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", value)
        )

    @staticmethod
    def styled_print(
            text: str,
            style: Literal[1, 4, 7] | None = None,
            fg: Literal[30, 31, 32, 33, 34, 35, 36, 37] | None = None,
            bg: Literal[40, 41, 42, 43, 44, 45, 46, 47] | None = None,
            reset: bool = True,
            end: str = "\n",
    ) -> None:
        """
        打印带有 ANSI 样式、前景色和背景色的文本。

        :param text: 输出文本
        :param style: 显示模式 (可选)
                    - 1: 加粗；
                    - 4: 下划线；
                    - 7: 反显；
                    - None: 不设置样式。
        :param fg: 前景色 (字体颜色) (可选)
                    - 30: 黑色；
                    - 31: 红色；
                    - 32: 绿色；
                    - 33: 黄色；
                    - 34: 蓝色；
                    - 35: 紫色；
                    - 36: 青色；
                    - 37: 白色；
                    - None: 不设置前景色。
        :param bg: 背景色 (可选)
                    - 40: 黑色；
                    - 41: 红色；
                    - 42: 绿色；
                    - 43: 黄色；
                    - 44: 蓝色；
                    - 45: 紫色；
                    - 46: 青色；
                    - 47: 白色；
                    - None: 不设置背景色。
        :param reset: 打印后是否恢复默认样式
        :param end: 结束符，默认为换行
        """
        codes = [str(c) for c in (style, fg, bg) if c is not None]  # 构建样式代码
        prefix = f"\033[{';'.join(codes)}m" if codes else ""  # 生成 ANSI 转义序列
        suffix = "\033[0m" if reset else ""

        print(f"{prefix}{text}{suffix}", end=end)


class _Clipboard(DelegatedBase[CharFuc]):
    """剪贴板工具"""

    @classmethod
    def read(cls) -> str:
        """
        读取剪贴板内容。

        :return: 剪贴板文本内容
        """
        return lazy_import("pyperclip").paste()

    @classmethod
    def write(cls, content: str) -> None:
        """
        将文本写入剪贴板。

        :param content: 待写入的文本
        """
        lazy_import("pyperclip").copy(content)

    @classmethod
    def monitor(cls, interval: float = 0.2) -> str:
        """
        监听剪贴板变化。

        当检测到剪贴板内容发生变化时，返回新的剪贴板内容。

        :param interval: 轮询间隔（秒）
        :return: 新的剪贴板内容
        """
        old_content = cls.read()

        while True:
            TimeFuc.sleep_seconds(interval)
            new_content = cls.read()
            if new_content and new_content != old_content:  # 剪贴板内容发生变化
                return new_content
