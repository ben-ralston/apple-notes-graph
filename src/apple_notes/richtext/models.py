"""Data models for Apple Notes richtext format.

The com.apple.notes.richtext format uses a protobuf structure inside an
NSKeyedArchive wrapper. These models represent the parsed structure.
"""

from dataclasses import dataclass, field
from enum import IntEnum


class FontStyle(IntEnum):
    """Font styling from protobuf field 5."""

    NORMAL = 0
    BOLD = 1
    ITALIC = 2
    BOLD_ITALIC = 3  # Both bold and italic


class ParagraphType(IntEnum):
    """Paragraph/block styling.

    WARNING: These enum values are logical identifiers, NOT raw protobuf field
    values. The protobuf wire format uses different values in some cases:
    - Protobuf 100 = BULLET_LIST (enum 103)
    - Protobuf 103 = CHECKLIST (enum 100)
    - Block quotes use protobuf field 8=1 rather than field 1

    The parser uses PARAGRAPH_TYPE_MAP to translate, and the markdown converter
    hardcodes the correct protobuf values in _build_paragraph_raw_data().
    Do NOT use .value as a protobuf field value directly.
    """

    DEFAULT = 0
    HEADING = 1
    SUBHEADING = 2
    MONOSPACE = 4  # Code block
    CHECKLIST = 100
    DASHED_LIST = 101
    NUMBERED_LIST = 102
    BULLET_LIST = 103
    BLOCK_QUOTE = 104


@dataclass
class ParagraphStyle:
    """Paragraph-level formatting (protobuf field 2 in AttributeRun)."""

    type: ParagraphType = ParagraphType.DEFAULT
    indent_level: int = 0
    checklist_checked: bool = False
    uuid: bytes | None = None

    # Raw protobuf data for fields we don't fully understand yet
    raw_data: dict = field(default_factory=dict)


@dataclass
class Attachment:
    """Embedded object like table or internal link (protobuf field 6)."""

    uuid: bytes
    type_uti: str  # e.g., "com.apple.notes.table"
    content: bytes | None = None  # For tables: cell content

    # Raw protobuf data for complete reconstruction
    raw_data: dict = field(default_factory=dict)


@dataclass
class AttributeRun:
    """A formatting span covering a range of characters (protobuf field 5).

    The sum of all run lengths MUST equal the text length.
    """

    length: int
    font_style: FontStyle = FontStyle.NORMAL
    underline: bool = False
    strikethrough: bool = False
    paragraph: ParagraphStyle | None = None

    # For colors: field 14 (highlight palette index) or field 10 (RGB)
    color: tuple[int, int, int] | None = None

    # For links (protobuf field 9)
    link_url: str | None = None

    # Raw protobuf data for complete reconstruction
    raw_data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.length < 0:
            raise ValueError(f"Run length must be non-negative, got {self.length}")


@dataclass
class NotesRichtext:
    """Complete representation of com.apple.notes.richtext content.

    Invariant: sum(run.length for run in runs) == len(text)
    """

    text: str
    runs: list[AttributeRun] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)

    # Original NSKeyedArchive structure for reconstruction
    _archive_template: dict | None = field(default=None, repr=False)

    # Original protobuf typedef for exact roundtrip encoding
    _protobuf_typedef: dict | None = field(default=None, repr=False)

    # Original NS.data bytes for comparison/debugging
    _original_ns_data: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate that run lengths sum to text length."""
        if not self.runs:
            return

        total_length = sum(run.length for run in self.runs)
        if total_length != len(self.text):
            raise ValueError(
                f"Run lengths sum ({total_length}) does not match "
                f"text length ({len(self.text)})"
            )

    def text_at_run(self, run_index: int) -> str:
        """Get the text covered by a specific run."""
        if run_index < 0 or run_index >= len(self.runs):
            raise IndexError(f"Run index {run_index} out of range")

        start = sum(r.length for r in self.runs[:run_index])
        end = start + self.runs[run_index].length
        return self.text[start:end]

    def run_at_position(self, position: int) -> tuple[int, AttributeRun]:
        """Find the run that contains a character position.

        Returns (run_index, run).
        """
        if position < 0 or position >= len(self.text):
            raise IndexError(f"Position {position} out of range")

        current_pos = 0
        for i, run in enumerate(self.runs):
            if current_pos + run.length > position:
                return i, run
            current_pos += run.length

        # Should not reach here if validate() passed
        raise RuntimeError("Position not found in any run")
