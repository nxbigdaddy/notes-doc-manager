"""Four immutable, code-defined templates; no file loading or expression engine.

Only {{title}}, {{date}} and {{datetime}} are substituted. A callback-based
single pass preserves replacement text literally, including braces/backslashes
inside a user's title. Markdown remains ordinary UTF-8 text for either .md/.txt.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from types import MappingProxyType
import unicodedata

from notes.domain.models import NotesError, TemplateInfo, TemplatePreview

MAX_TITLE_CHARACTERS = 255
MAX_TEMPLATE_BYTES = 64 * 1024
_TOKENS = re.compile(r"\{\{([^{}]+)\}\}")
_ALLOWED_TOKENS = frozenset({"title", "date", "datetime"})
_BIDI_CONTROLS = frozenset({
    "\u061c", "\u200e", "\u200f",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2066", "\u2067", "\u2068", "\u2069",
})


@dataclass(frozen=True)
class _Template:
    info: TemplateInfo
    source: str


_TEMPLATES = MappingProxyType({
    "blank": _Template(
        TemplateInfo("blank", "空白文档", "创建空文件，不预填正文。", ".txt"),
        "",
    ),
    "meeting": _Template(
        TemplateInfo("meeting", "会议纪要", "记录议题、讨论、决定与待办。"),
        "# {{title}}\n\n"
        "日期：{{date}}\n"
        "记录时间：{{datetime}}\n"
        "参与人员：\n\n"
        "## 议题\n\n- \n\n"
        "## 讨论记录\n\n- \n\n"
        "## 决定与待办\n\n- [ ] \n",
    ),
    "daily": _Template(
        TemplateInfo("daily", "每日记录", "整理今日计划、工作记录及总结。"),
        "# {{title}}\n\n"
        "日期：{{date}}\n\n"
        "## 今日计划\n\n- [ ] \n\n"
        "## 工作记录\n\n- \n\n"
        "## 总结与明日安排\n\n- \n",
    ),
    "troubleshooting": _Template(
        TemplateInfo("troubleshooting", "问题排查", "记录现象、复现步骤、排查和验证。"),
        "# {{title}}\n\n"
        "记录时间：{{datetime}}\n\n"
        "## 问题现象\n\n- \n\n"
        "## 环境与复现步骤\n\n1. \n\n"
        "## 排查记录\n\n- \n\n"
        "## 解决方案与验证\n\n- [ ] \n",
    ),
})
_TEMPLATE_INFOS = tuple(template.info for template in _TEMPLATES.values())


def list_templates() -> tuple[TemplateInfo, ...]:
    """Return the fixed registry in display order; entries are frozen DTOs."""
    return _TEMPLATE_INFOS


def _validate_title(title):
    if (type(title) is not str or not 1 <= len(title) <= MAX_TITLE_CHARACTERS
            or not title.strip()):
        raise NotesError("invalid_title", "标题应为 1 至 255 个有效字符，且不能只有空白。")
    for character in title:
        if (unicodedata.category(character) in {"Cc", "Cs"}
                or character in {"\u2028", "\u2029", "\ufeff"}
                or character in _BIDI_CONTROLS):
            raise NotesError("invalid_title", "标题不能包含控制字符、换行、BOM、双向控制或无效 Unicode。")
    # No trimming/NFC/case folding: a validated filename stem is display text,
    # and the renderer must not silently alter that identity.


def _snapshot_time(now):
    if now is None:
        try:
            now = datetime.now().astimezone()
        except Exception:
            raise NotesError("invalid_date", "无法取得有效的系统本地时间。") from None
    if type(now) is not datetime:
        raise NotesError("invalid_date", "模板时间必须是带时区的有效 datetime。")
    try:
        offset = now.utcoffset()
        if now.tzinfo is None or offset is None:
            raise ValueError("timezone required")
        # Freeze the supplied offset once; a rendering should not consult a
        # timezone provider repeatedly or convert the caller's calendar date.
        snapshot = now.replace(tzinfo=timezone(offset))
        rendered_at = snapshot.isoformat()
        date_text = snapshot.date().isoformat()
        datetime_text = snapshot.isoformat(sep=" ", timespec="seconds")
    except Exception:
        raise NotesError("invalid_date", "模板时间或时区无效。") from None
    return rendered_at, date_text, datetime_text


def render_template(template_id: str, title: str, now: datetime | None = None) -> TemplatePreview:
    """Render one built-in template at one explicit/local aware timestamp.

    This does not validate a filesystem destination, create a document, open an
    editor, import templates or execute template/title text.
    """
    if type(template_id) is not str or template_id not in _TEMPLATES:
        raise NotesError("invalid_template", "未知的内置模板，请从提供的模板列表中选择。")
    _validate_title(title)
    rendered_at, date_text, datetime_text = _snapshot_time(now)
    template = _TEMPLATES[template_id]
    values = {"title": title, "date": date_text, "datetime": datetime_text}

    def replacement(match):
        token = YOUR_SECRET)
        if token not in _ALLOWED_TOKENS:
            raise NotesError("invalid_template", "内置模板包含不支持的占位符。")
        return values[token]

    # Inspect only the immutable source before substitution. Never parse the
    # replacement title again, even if it contains valid-looking placeholders.
    residue = _TOKENS.sub("", template.source)
    if "{{" in residue or "}}" in residue:
        raise NotesError("invalid_template", "内置模板占位符格式无效。")
    for match in _TOKENS.finditer(template.source):
        if ((match.start() and template.source[match.start() - 1] == "{")
                or (match.end() < len(template.source) and template.source[match.end()] == "}")):
            raise NotesError("invalid_template", "内置模板不支持嵌套占位符。")
    text = _TOKENS.sub(replacement, template.source)
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise NotesError("invalid_template", "模板不能生成有效的 UTF-8 文本。") from None
    if "\r" in text or "\ufeff" in text:
        raise NotesError("invalid_template", "模板正文必须使用 LF 换行且不能含 BOM。")
    if len(encoded) > MAX_TEMPLATE_BYTES:
        raise NotesError("template_too_large", "模板正文超过 64 KiB UTF-8 大小上限。")
    return TemplatePreview(template_id, title, text, rendered_at)
