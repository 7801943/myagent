"""
StructuredOutputParser：从 LLM 文本输出中提取结构化数据。

注意：原 StreamParser（StreamEvent → Hook 分发）已合并入 StreamProcessor（stream.py）。
本文件仅保留 StructuredOutputParser。
"""
import json
import re as _re
from typing import Any, Callable


class StructuredOutputParser:
    """
    从 LLM 文本输出中提取结构化数据。
    支持：
    1. Markdown 代码块提取 (```json ... ```)
    2. 纯 JSON 文本解析
    3. 自定义格式注册
    """

    _CODEBLOCK_RE = _re.compile(r"```(\w*)\n(.*?)```", _re.DOTALL)

    def __init__(self):
        self._parsers: dict[str, Callable[[str], Any]] = {}

    def register(self, format_name: str, parser_fn: Callable[[str], Any]) -> None:
        """注册自定义解析器。"""
        self._parsers[format_name] = parser_fn

    def extract_json(self, text: str) -> dict | list | None:
        """
        尝试从文本中提取 JSON 内容。
        优先匹配代码块，回退到整段文本。
        """
        # 先尝试代码块
        for match in self._CODEBLOCK_RE.finditer(text):
            lang, content = match.group(1), match.group(2)
            if lang in ("json", ""):
                try:
                    return json.loads(content.strip())
                except json.JSONDecodeError:
                    continue

        # 再尝试整段文本
        text_stripped = text.strip()
        end_chars = {"{": "}", "[": "]"}
        for start_char in ["{", "["]:
            idx = text_stripped.find(start_char)
            if idx >= 0:
                end_char = end_chars[start_char]
                end_idx = text_stripped.rfind(end_char)
                if end_idx >= idx:
                    try:
                        return json.loads(text_stripped[idx:end_idx + 1])
                    except json.JSONDecodeError:
                        pass
                # 回退：没有找到正确的闭合或解析失败，尝试暴力 parse 到结尾
                try:
                    return json.loads(text_stripped[idx:])
                except json.JSONDecodeError:
                    continue

        return None

    def extract_codeblocks(self, text: str, language: str | None = None) -> list[str]:
        """
        提取所有代码块的内容。
        language: 指定语言时只提取该语言的代码块，None 则提取全部。
        """
        blocks = []
        for match in self._CODEBLOCK_RE.finditer(text):
            lang, content = match.group(1), match.group(2)
            if language is None or lang == language:
                blocks.append(content.strip())
        return blocks

    def parse(self, text: str, format_name: str) -> Any:
        """使用注册的解析器解析文本。"""
        if format_name in self._parsers:
            return self._parsers[format_name](text)
        if format_name == "json":
            return self.extract_json(text)
        raise ValueError(f"Unknown format: {format_name}")