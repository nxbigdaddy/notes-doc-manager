from dataclasses import FrozenInstanceError
import json
import uuid

import pytest

from notes.domain.models import NotesError, SearchQuery, SmartList
from notes.domain.smart_lists import (
    decode_query, encode_query, query_payload, validate_name, validate_query,
)

LIBRARY = str(uuid.UUID(int=1))
TAG = str(uuid.UUID(int=2))


def test_round_trip_is_bounded_and_excludes_pagination():
    query = SearchQuery(text="中文 ' OR 1=1 --", mode="content", library_id=LIBRARY,
                        folder=" leading/子目录", tag_ids=(TAG,), favorites_only=True, page=8)
    encoded = encode_query(query)
    result = decode_query(encoded)
    assert result == SearchQuery(**dict(query_payload(query), tag_ids=(TAG,)))
    assert result.page == 0 and result.page_size == 100
    assert encoded == encode_query(result)
    assert len(encoded.encode("utf-8")) <= 8192
    assert result.folder.startswith(" ")


def test_empty_query_and_duplicate_and_tags():
    assert validate_query({}) == SearchQuery()
    assert validate_query({"tag_ids": [TAG, TAG]}).tag_ids == (TAG,)
    assert validate_query({"library_id": LIBRARY, "folder": "x\\y"}).folder == "x/y"


@pytest.mark.parametrize("value", [
    None, [], {"sql": "SELECT *"}, {"page": 1}, {"text": None}, {"text": "x" * 257},
    {"text": "\ud800"}, {"text": "\x00"}, {"mode": "body"}, {"mode": []},
    {"library_id": ""}, {"library_id": True}, {"library_id": LIBRARY.upper() + "x"},
    {"tag_ids": TAG}, {"tag_ids": [None]}, {"tag_ids": [TAG] * 33},
    {"favorites_only": 1}, {"recent_only": "false"}, {"include_unavailable": None},
    {"folder": "without-library"},
])
def test_unsafe_or_ambiguous_queries_are_not_silently_coerced(value):
    with pytest.raises(NotesError):
        validate_query(value)


@pytest.mark.parametrize("folder", [
    "../escape", "a/../b", "a//b", "./a", ".", "a/", "/a", "\\a", "C:\\a", "C:a",
    "//server/share", "a:stream", "bad\x00", "CON.txt", "CON .txt", "CONIN$",
    "a.", "a ", "x/\ud800", "x" * 256, "😀" * 128, 12,
])
def test_folders_fail_lexically_without_filesystem_access(folder):
    with pytest.raises(NotesError):
        validate_query({"library_id": LIBRARY, "folder": folder})


@pytest.mark.parametrize("value", [
    "", "{}", "[]", '{"version":true,"query":{}}', '{"version":2,"query":{}}',
    '{"version":1,"query":{},"sql":"SELECT"}',
    '{"version":1,"version":1,"query":{}}',
    '{"version":1,"query":{"mode":"all","mode":"content"}}',
    '{"version":1,"query":{"text":NaN}}', '{"version":1,"query":{"unknown":true}}',
    '{"version":1,"query":{"text":"\\ud800"}}', " " * 8193, "\ud800", None,
])
def test_malformed_persistence_is_rejected_without_reset(value):
    with pytest.raises(NotesError):
        decode_query(value)


@pytest.mark.parametrize("name", ["", " ", "x" * 81, "x\nx", "a\n", "\ta", "a\u202eb", "\ud800", None])
def test_invalid_names(name):
    with pytest.raises(NotesError):
        validate_name(name)


def test_names_and_dtos_do_not_execute_or_normalize_paths():
    assert validate_name("  中文列表 👨‍👩‍👧‍👦  ") == "中文列表 👨‍👩‍👧‍👦"
    item = SmartList(LIBRARY, "名称", decode_query(json.dumps({"version": 1, "query": {}})))
    with pytest.raises(FrozenInstanceError):
        item.name = "changed"
