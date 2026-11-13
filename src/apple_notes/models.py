"""Data models for the apple_notes library."""

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum


class AttachmentType(Enum):
    """Types of attachments in Apple Notes."""

    LINK = "com.apple.notes.inlinetextattachment.link"
    TABLE = "com.apple.notes.table"
    DRAWING = "com.apple.drawing.2"
    HASHTAG = "com.apple.notes.inlinetextattachment.hashtag"
    MENTION = "com.apple.notes.inlinetextattachment.mention"
    GALLERY = "com.apple.notes.gallery"
    IMAGE_JPEG = "public.jpeg"
    IMAGE_PNG = "public.png"
    IMAGE_HEIC = "public.heic"
    URL = "public.url"
    UNKNOWN = "unknown"

    @classmethod
    def from_uti(cls, uti: str) -> "AttachmentType":
        """Get AttachmentType from UTI string."""
        for member in cls:
            if member.value == uti:
                return member
        return cls.UNKNOWN


@dataclass
class Note:
    """Basic note metadata."""

    name: str
    uuid: str
    folder: str
    pk: int
    created: Optional[str] = None  # ISO 8601 timestamp
    modified: Optional[str] = None  # ISO 8601 timestamp
    last_opened: Optional[str] = None  # ISO 8601 timestamp


@dataclass(frozen=True)
class TitleMatch:
    """One note carrying a title, wherever it lives (#229 delete/move guards).

    Includes notes in Recently Deleted (``in_trash``) and locked notes, so the
    guards can refuse them by name instead of silently skipping them.
    """

    pk: int
    uuid: str
    title: str
    folder: str
    folder_pk: Optional[int]
    modified: Optional[str]
    in_trash: bool
    locked: bool

    def as_note(self) -> "Note":
        return Note(
            name=self.title,
            uuid=self.uuid,
            folder=self.folder,
            pk=self.pk,
            modified=self.modified,
        )


@dataclass(frozen=True)
class FolderMatch:
    """A folder a note can be moved into (#230)."""

    pk: int
    name: str
    path: str


@dataclass
class NoteLink:
    """A link from one note to another."""

    target_note_name: str
    target_uuid: str
    position: int
    alt_text: Optional[str] = None


@dataclass
class ExternalLink:
    """An external URL link in a note."""

    url: str
    text: str
    position: int
    length: int


@dataclass
class Attachment:
    """An attachment in a note (image, table, drawing, etc.)."""

    identifier: str
    type_uti: str
    position: int
    alt_text: Optional[str] = None

    @property
    def attachment_type(self) -> AttachmentType:
        """Get the AttachmentType enum value."""
        return AttachmentType.from_uti(self.type_uti)

    @property
    def type_label(self) -> str:
        """Get a human-readable label for this attachment type."""
        type_map = {
            "com.apple.notes.inlinetextattachment.link": "link",
            "com.apple.notes.table": "table",
            "com.apple.drawing.2": "drawing",
            "com.apple.notes.inlinetextattachment.hashtag": "hashtag",
            "com.apple.notes.inlinetextattachment.mention": "mention",
            "public.jpeg": "image",
            "public.png": "image",
            "public.heic": "image",
            "public.url": "url",
            "com.apple.notes.gallery": "gallery",
        }

        if self.type_uti in type_map:
            return type_map[self.type_uti]

        if self.type_uti and self.type_uti.startswith("public."):
            return "file"

        return self.type_uti or "attachment"


@dataclass
class GraphLink:
    """A link between two notes for graph visualization."""

    source_name: str
    target_name: str
    source_pk: int
    target_pk: int


@dataclass
class RawNoteLink:
    """An unclassified note-link attachment row from the database."""

    source_pk: int
    target_url: str
    alt_text: Optional[str] = None


@dataclass
class BrokenLink:
    """A note link whose target no longer resolves to an active note."""

    source_name: str
    source_folder: str
    target_uuid: str
    intended_target_title: Optional[str]  # ZALTTEXT at link creation; may be None
    occurrences: int = 1
    source_pk: Optional[int] = None  # unambiguous source id (names can collide)


@dataclass
class NoteContent:
    """Complete note content with resolved links."""

    name: str
    uuid: str
    folder: str
    raw_text: str
    resolved_text: str
    note_links: List[NoteLink] = field(default_factory=list)
    external_links: List[ExternalLink] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
    created: Optional[str] = None  # ISO 8601 timestamp
    modified: Optional[str] = None  # ISO 8601 timestamp
    last_opened: Optional[str] = None  # ISO 8601 timestamp
