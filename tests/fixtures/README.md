# Test Fixtures

Sample files for testing Apple Notes format parsing.

## Notes Richtext Samples (`com.apple.notes.richtext`)

These are real samples captured from Apple Notes clipboard. They contain the same test note content with one text difference:

- `notes_richtext_sample_with_great_sentence.bin` - contains "great sentence"
- `notes_richtext_sample_with_amazing_phrase.bin` - contains "amazing phrase"

The test note includes:
- Bold, italic, underlined, strikethrough text
- Headers (H1, H2, H3)
- Code blocks
- Bullet lists, numbered lists, dashed lists
- Checklists with checked and unchecked items
- Block quotes
- Tables
- Colored text (pink, mint)
- Internal note links `[[Note Name]]`
- External hyperlinks

### Format Details

The `com.apple.notes.richtext` format is:
- An NSKeyedArchive (binary plist containing archived Objective-C objects)
- Contains Apple Notes' native content representation
- Preserves ALL formatting when round-tripped through clipboard

See issue #31 for work on fully parsing this format.
