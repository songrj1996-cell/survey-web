"""core/text:共享的纯文本工具(无业务、无 I/O)。"""
import re


def _short_text(value: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."
