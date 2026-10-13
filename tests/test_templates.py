"""Pure built-in rendering tests: no files, user templates, editors or dependencies."""
import builtins
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone, tzinfo
from types import MappingProxyType

import pytest

from notes.domain.models import NotesError, TemplatePreview
from notes.domain import templates


NOW = datetime(2026, 9, 16, 14, 5, 6, 123456, tzinfo=timezone(timedelta(hours=8)))
IDS = ("blank", "meeting", "daily", "troubleshooting")


def test_registry_is_finite_ordered_and_immutable():
    assert templates.MAX_TITLE_CHARACTERS == 255
    assert templates.MAX_TEMPLATE_BYTES == 64 * 1024
    entries = templates.list_templates()
    assert isinstance(entries, tuple)
    assert tuple(entry.id for entry in entries) == IDS
    assert entries[0].suggested_extension == ".txt"
    assert all(entry.suggested_extension == ".md" for entry in entries[1:])
    assert all(entry.name and entry.description for entry in entries)
    with pytest.raises(FrozenInstanceError):
        entries[0].name = "changed"
    with pytest.raises(TypeError):
        templates._TEMPLATES["custom"] = templates._TEMPLATES["daily"]
    with pytest.raises(FrozenInstanceError):
        templates._TEMPLATES["daily"].source = "changed"
    assert templates.list_templates() == entries


@pytest.mark.parametrize("identifier", IDS)
def test_render_is_deterministic_utf8_lf_bounded_and_frozen(identifier):
    first = templates.render_template(identifier, "中文记录 🚀", NOW)
    assert isinstance(first, TemplatePreview)
    assert first == templates.render_template(identifier, "中文记录 🚀", NOW)
    assert first.template_id == identifier and first.title == "中文记录 🚀"
    assert first.rendered_at == NOW.isoformat()
    encoded = first.text.encode("utf-8")
    assert encoded.decode("utf-8") == first.text
    assert len(encoded) <= 64 * 1024 and not encoded.startswith(b"\xef\xbb\xbf")
    assert "\r" not in first.text and "\ufeff" not in first.text
    with pytest.raises(FrozenInstanceError):
        first.text = "mutated"
    if identifier == "blank":
        assert first.text == "" and encoded == b""
    else:
        assert first.text.startswith("# 中文记录 🚀\n")
        assert "{{date}}" not in first.text and "{{datetime}}" not in first.text


@pytest.mark.parametrize("title", [
    "{{date}}", "{{datetime}}", "{{title}}",
    "{{title.__class__.__mro__}}", "{{__import__('os').system('do-not-run')}}",
    r"报告\1\g<title>\n{{date}}", "${HOME} {title!r:99}", " Cafe\u0301 ",
])
def test_title_substitution_is_literal_single_pass(title):
    result = templates.render_template("meeting", title, NOW)
    assert result.title == title
    assert result.text.split("\n")[0] == "# " + title
    assert "日期：2026-09-16" in result.text
    assert "记录时间：2026-09-16 14:05:06+08:00" in result.text


@pytest.mark.parametrize("title", ["👩\u200d💻", "👩\u200d👩\u200d👦", "❤️", "字\U000e0100", "Cafe\u0301"])
def test_legitimate_unicode_sequences_are_not_normalized_or_rejected(title):
    result = templates.render_template("daily", title, NOW)
    assert result.title == title and result.text.splitlines()[0] == "# " + title


@pytest.mark.parametrize("identifier", [
    None, True, 0, [], {}, "", "Daily", "DAILY", "daily ", " custom",
    "../daily", "C:\\untrusted\\template.md", "__import__('os')", "{{daily}}", "\ud800",
])
def test_unknown_or_non_string_identifiers_are_rejected(identifier):
    with pytest.raises(NotesError) as error:
        templates.render_template(identifier, "安全标题", NOW)
    assert error.value.code == "invalid_template"


@pytest.mark.parametrize("title", [
    None, 1, False, [], "", "   ", "\t", "one\nline", "one\rline",
    "nul\0inside", "\x7f", "\x85", "\ud800", "\udfff",
    "first\u2028second", "first\u2029second", "\ufefftitle", "title\ufeff",
    "bidirectional\u202e", "\u2066spoof\u2069", "\u061c", "\u200f",
])
def test_invalid_titles_rejected_even_for_blank_preview(title):
    for identifier in ("blank", "daily"):
        with pytest.raises(NotesError) as error:
            templates.render_template(identifier, title, NOW)
        assert error.value.code == "invalid_title"


def test_title_limit_accepts_long_stems_and_counts_unicode_scalars():
    for length in (230, 255):
        title = "文" * length
        assert templates.render_template("blank", title, NOW).text == ""
        assert templates.render_template("daily", title, NOW).title == title
    # Domain text counts Unicode scalars; actual filesystem filename limits are
    # separately enforced by the caller/platform.
    assert templates.render_template("daily", "🚀" * 255, NOW).title == "🚀" * 255
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "文" * 256, NOW)
    assert error.value.code == "invalid_title"


@pytest.mark.parametrize("value", [
    "2026-02-30", "2026-09-16T00:00:00+08:00", 0, 1.5, False, [], {},
    date(2026, 9, 16), datetime(2026, 9, 16),
])
def test_invalid_or_naive_dates_are_rejected(value):
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "记录", value)
    assert error.value.code == "invalid_date"


@pytest.mark.parametrize("offset", [None, 1, timedelta(hours=24)])
def test_invalid_timezone_protocol_is_rejected(offset):
    class BadTimezone(tzinfo):
        def utcoffset(self, dt):
            return offset
    value = datetime(2026, 9, 16, tzinfo=BadTimezone())
    with pytest.raises(NotesError) as error:
        templates.render_template("meeting", "记录", value)
    assert error.value.code == "invalid_date"


def test_timezone_provider_failure_is_reported_as_invalid_date_without_details():
    class FailingTimezone(tzinfo):
        def utcoffset(self, dt):
            raise RuntimeError("private provider details must not escape")
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "记录", datetime(2026, 9, 16, tzinfo=FailingTimezone()))
    assert error.value.code == "invalid_date"
    assert "private provider" not in error.value.message


def test_single_render_freezes_timezone_offset_once():
    class CountingTimezone(tzinfo):
        calls = 0
        def utcoffset(self, dt):
            self.calls += 1
            return timedelta(hours=8 if self.calls == 1 else 9)
    provider = CountingTimezone()
    result = templates.render_template("meeting", "记录", datetime(2026, 9, 16, 8, 30, tzinfo=provider))
    assert provider.calls == 1
    assert result.rendered_at == "2026-09-16T08:30:00+08:00"
    assert "2026-09-16 08:30:00+08:00" in result.text


def test_datetime_subclasses_cannot_supply_rendering_methods():
    class TrapDate(datetime):
        def isoformat(self, *args, **kwargs):
            raise AssertionError("must not invoke user-supplied date formatting")
    value = TrapDate(2026, 9, 16, tzinfo=timezone.utc)
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "记录", value)
    assert error.value.code == "invalid_date"


def test_string_subclasses_are_not_evaluated_or_hashed():
    class TrapString(str):
        def __hash__(self):
            raise AssertionError("must not call an untrusted hash implementation")
        def strip(self, *args):
            raise AssertionError("must not call an untrusted title implementation")
    with pytest.raises(NotesError):
        templates.render_template(TrapString("daily"), "记录", NOW)
    with pytest.raises(NotesError):
        templates.render_template("daily", TrapString("记录"), NOW)


def test_supplied_timezone_calendar_and_midnight_are_respected():
    east = timezone(timedelta(hours=14))
    before = datetime(2026, 9, 16, 23, 59, 59, tzinfo=east)
    after = datetime(2026, 9, 17, 0, 0, 1, tzinfo=east)
    old_preview = templates.render_template("daily", "每日记录", before)
    fresh_creation = templates.render_template("daily", "每日记录", after)
    assert "日期：2026-09-16" in old_preview.text
    assert "日期：2026-09-17" in fresh_creation.text
    assert datetime.fromisoformat(fresh_creation.rendered_at) == after
    assert fresh_creation.rendered_at.endswith("+14:00")
    # The same instant at UTC has a different calendar date. The supplied local
    # calendar is deliberate, not silently converted to the machine's timezone.
    utc = templates.render_template("daily", "每日记录", after.astimezone(timezone.utc))
    assert "日期：2026-09-16" in utc.text


def test_default_timestamp_is_aware_and_matches_its_own_rendered_date():
    preview = templates.render_template("daily", "本地日期")
    value = datetime.fromisoformat(preview.rendered_at)
    assert value.tzinfo is not None and value.utcoffset() is not None
    assert "日期：" + value.date().isoformat() in preview.text


def test_utf8_limit_uses_bytes_not_character_count(monkeypatch):
    expected = templates.render_template("daily", "中文标题", NOW)
    size = len(expected.text.encode("utf-8"))
    assert size > len(expected.text)
    monkeypatch.setattr(templates, "MAX_TEMPLATE_BYTES", size)
    assert templates.render_template("daily", "中文标题", NOW) == expected
    monkeypatch.setattr(templates, "MAX_TEMPLATE_BYTES", size - 1)
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "中文标题", NOW)
    assert error.value.code == "template_too_large"


@pytest.mark.parametrize("source", [
    "{{unknown}}", "{{title.__class__}}", "{{date:%Y}}", "{{ title }}",
    "{{__import__('os').system('unsafe')}}", "{{{date}}}", "{{date", "date}}",
])
def test_even_accidental_nonwhitelisted_builtin_syntax_fails_closed(monkeypatch, source):
    # This is a developer regression injection, not a public custom-template API.
    original = templates._TEMPLATES["daily"]
    altered = templates._Template(original.info, source)
    monkeypatch.setattr(templates, "_TEMPLATES", MappingProxyType({"daily": altered}))
    with pytest.raises(NotesError) as error:
        templates.render_template("daily", "记录", NOW)
    assert error.value.code == "invalid_template"


def test_render_never_opens_files_evaluates_or_executes_title(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("template rendering must not execute code or open files")
    title = "{{__import__('os').system('never-execute')}}"
    with monkeypatch.context() as changes:
        changes.setattr(builtins, "open", forbidden)
        changes.setattr(builtins, "eval", forbidden)
        changes.setattr(builtins, "exec", forbidden)
        outputs = [templates.render_template(identifier, title, NOW) for identifier in IDS]
    assert outputs[0].text == ""
    assert all(result.text.startswith("# " + title + "\n") for result in outputs[1:])
