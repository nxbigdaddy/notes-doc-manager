"""Shared immutable contracts between storage, filesystem, services and GUI."""

from dataclasses import dataclass
from pathlib import Path


class NotesError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    root_path: str
    enabled: bool = True
    availability: str = "unknown"
    extensions: tuple[str, ...] = (".txt", ".md")
    excluded_dirs: tuple[str, ...] = (".git", ".venv", "__pycache__")


@dataclass(frozen=True)
class Tag:
    id: str
    name: str


@dataclass(frozen=True)
class Document:
    id: str
    library_id: str
    relative_path: str
    file_name: str
    size_bytes: int = 0
    modified_at: int = 0
    availability: str = "present"
    in_scope: bool = True
    index_status: str = "pending"
    content_generation: int = 1
    is_favorite: bool = False
    last_open_requested_at: int | None = None
    encoding_override: str | None = None
    tags: tuple[Tag, ...] = ()


@dataclass(frozen=True)
class FileEntry:
    relative_path: str
    size_bytes: int
    modified_at: int
    file_identity: str = ""


@dataclass(frozen=True)
class MoveHint:
    source_relative: str
    target_relative: str
    received_at: float
    is_directory: bool = False
    is_synthetic: bool = False


@dataclass(frozen=True)
class ChangeBatch:
    paths: tuple[str, ...] | None = ()
    moves: tuple[MoveHint, ...] = ()
    reliable: bool = True


@dataclass(frozen=True)
class ScanResult:
    entries: tuple[FileEntry, ...] = ()
    complete: bool = True
    availability: str = "online"
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadResult:
    text: str = ""
    encoding: str = ""
    status: str = "ready"
    truncated: bool = False
    size_bytes: int = 0
    modified_at: int = 0
    message: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class SearchQuery:
    text: str = ""
    mode: str = "all"
    library_id: str | None = None
    folder: str = ""
    tag_ids: tuple[str, ...] = ()
    favorites_only: bool = False
    recent_only: bool = False
    include_unavailable: bool = False
    page: int = 0
    page_size: int = 100


@dataclass(frozen=True)
class SearchHit:
    document: Document
    snippet: str = ""
    line: int | None = None
    matched_name: bool = False


@dataclass(frozen=True)
class SearchResult:
    items: tuple[SearchHit, ...] = ()
    total: int = 0
    covered: int = 0
    uncovered: int = 0
    complete: bool = True
    warning: str = ""


@dataclass(frozen=True)
class Preview:
    document: Document
    path: str
    read: ReadResult


@dataclass(frozen=True)
class TemplateInfo:
    id: str
    name: str
    description: str
    suggested_extension: str = ".md"


@dataclass(frozen=True)
class TemplatePreview:
    template_id: str
    title: str
    text: str
    rendered_at: str


@dataclass(frozen=True)
class SmartList:
    id: str
    name: str
    query: SearchQuery
    available: bool = True
    warning: str = ""


@dataclass(frozen=True)
class AppState:
    libraries: tuple[Library, ...] = ()
    tags: tuple[Tag, ...] = ()
    editor_path: str = ""
    status: str = "就绪"
    busy: bool = False
    data_revision: int = 0
    backup_status: str = ""
    last_backup_at: float | None = None
    quick_open_hotkey_enabled: bool = False
    smart_lists: tuple[SmartList, ...] = ()
    smart_lists_warning: str = ""


def document_path(library: Library, document: Document) -> Path:
    """Convenience only; filesystem operations must separately validate boundaries."""
    return Path(library.root_path) / document.relative_path
