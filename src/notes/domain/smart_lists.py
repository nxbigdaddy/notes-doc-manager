"""Bounded, versioned saved filters. No SQL, filesystem access or execution."""
from dataclasses import asdict
import json
import ntpath
import unicodedata
import uuid

from notes.domain.models import NotesError, SearchQuery

MAX_SMART_LISTS = 100
MAX_SMART_TAGS = 32
MAX_QUERY_BYTES = 8192
QUERY_FIELDS = frozenset({
    "text", "mode", "library_id", "folder", "tag_ids",
    "favorites_only", "recent_only", "include_unavailable",
})
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$",
            *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}


def _invalid(message="智能列表筛选格式无效；请检查条件，不会执行或丢弃未知条件。"):
    return NotesError("invalid_smart_list", message)


def validate_name(value) -> str:
    if type(value) is not str:
        raise _invalid("智能列表名称应为文本。")
    if any(
        unicodedata.category(char) in {"Cc", "Cs"} or char in _BIDI
        or char in "\ufeff\u2028\u2029" for char in value
    ):
        raise _invalid("智能列表名称应为1至80个有效字符，不能包含控制字符。")
    value = value.strip()
    if not 1 <= len(value) <= 80:
        raise _invalid("智能列表名称应为1至80个有效字符，不能包含控制字符。")
    return value


def _reference(value):
    if type(value) is not str:
        raise _invalid("文档库和标签必须使用有效的标识。")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise _invalid("文档库或标签标识无效。") from None
    return value


def _folder(value, library_id):
    if type(value) is not str or len(value) > 1024:
        raise _invalid("文件夹应为不超过1024字符的库内相对路径。")
    if not value:
        return ""
    if library_id is None or value.startswith(("/", "\\")) or ntpath.splitdrive(value)[0]:
        raise _invalid("文件夹筛选必须指定文档库，并使用库内相对路径。")
    value = value.replace("\\", "/")
    for part in value.split("/"):
        if (not part or part in {".", ".."} or part.endswith((" ", "."))
                or any(ord(c) < 32 or c in '<>:"/\\|?*' or 0xD800 <= ord(c) <= 0xDFFF
                       for c in part)
                or part.split(".", 1)[0].rstrip(" ").upper() in _DEVICES):
            raise _invalid("文件夹筛选包含不合法的路径组件。")
        if len(part.encode("utf-16-le")) // 2 > 255:
            raise _invalid("文件夹名称超过Windows路径组件长度限制。")
    return value


def validate_query(value: dict) -> SearchQuery:
    if type(value) is not dict or set(value) - QUERY_FIELDS:
        raise _invalid("智能列表只接受已支持的筛选字段，不能包含SQL、分页或未知字段。")
    text = value.get("text", "")
    if (type(text) is not str or len(text) > 256 or "\x00" in text
            or any(0xD800 <= ord(c) <= 0xDFFF for c in text)):
        raise _invalid("搜索词应为最多256个有效字符。")
    mode = value.get("mode", "all")
    if type(mode) is not str or mode not in {"all", "name", "content"}:
        raise _invalid("智能列表搜索模式无效。")
    library_id = value.get("library_id")
    if library_id is not None:
        _reference(library_id)
    folder = _folder(value.get("folder", ""), library_id)
    tags = value.get("tag_ids", ())
    if type(tags) not in (list, tuple) or len(tags) > MAX_SMART_TAGS:
        raise _invalid("智能列表最多支持32个标签条件。")
    tags = tuple(dict.fromkeys(_reference(tag) for tag in tags))
    flags = {key: value.get(key, False) for key in
             ("favorites_only", "recent_only", "include_unavailable")}
    if any(type(flag) is not bool for flag in flags.values()):
        raise _invalid("智能列表筛选开关必须是布尔值。")
    return SearchQuery(text=text, mode=mode, library_id=library_id, folder=folder,
                       tag_ids=tags, **flags)


def query_payload(query: SearchQuery) -> dict:
    if not isinstance(query, SearchQuery):
        raise _invalid()
    result = {key: value for key, value in asdict(query).items() if key in QUERY_FIELDS}
    result["tag_ids"] = list(query.tag_ids)
    return result


def encode_query(value: dict | SearchQuery) -> str:
    query = validate_query(query_payload(value) if isinstance(value, SearchQuery) else value)
    encoded = json.dumps({"version": 1, "query": query_payload(query)},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_QUERY_BYTES:
        raise _invalid("智能列表条件超过8KiB上限。")
    return encoded


def decode_query(value: str) -> SearchQuery:
    if type(value) is not str:
        raise _invalid()
    try:
        if len(value.encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result

        def invalid_constant(_value):
            raise ValueError("non-finite number")

        envelope = json.loads(value, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if (type(envelope) is not dict or set(envelope) != {"version", "query"}
                or type(envelope["version"]) is not int or envelope["version"] != 1):
            raise ValueError
        return validate_query(envelope["query"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _invalid("保存的智能列表版本或内容无效；不会清空或放宽筛选。") from None
