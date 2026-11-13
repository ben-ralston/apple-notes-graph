"""A resolved [[link]]'s target uuid travels with its pending action (#185).

The Cmd+K flow these actions fed is gone (issue #212: links are synthesized
as attachments, see test_synth_links.py), but the converter still records the
uuid on the intermediate action and the daemon wire format still carries it.

No GUI: keystrokes and the editor copy-back are mocked, so this module carries
no `macos_gui` marker and runs in CI.
"""



from apple_notes.richtext import PendingKeyboardAction, markdown_to_richtext_new
from apple_notes.ui_helper_client import _serialize_pending_actions
from apple_notes.ui_helper_daemon import _deserialize_pending_actions

UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
UUID_B = "BBBBBBBB-1111-2222-3333-444444444444"


def _link(name: str, uuid: str | None, index: int = 0) -> PendingKeyboardAction:
    return PendingKeyboardAction(
        placeholder=f"{{__NG_LINK_{index}__}}",
        action_type="wiki_link",
        text_to_type=name,
        original_markdown=f"[[{name}]]",
        target_uuid=uuid,
    )


class TestTargetUuidCarried:
    def test_converter_records_the_resolved_uuid(self):
        result = markdown_to_richtext_new(
            "before [[Spike A]] after",
            link_resolver=lambda n: UUID_A.lower() if n == "Spike A" else None,
        )
        (action,) = result.pending_actions
        assert action.action_type == "wiki_link"
        assert action.target_uuid == UUID_A

    def test_daemon_round_trip_keeps_the_uuid(self):
        actions = [_link("Spike A", UUID_A)]
        restored = _deserialize_pending_actions(_serialize_pending_actions(actions))
        assert restored[0].target_uuid == UUID_A

    def test_daemon_accepts_actions_without_a_uuid(self):
        # An older client that predates the field must not break the daemon.
        raw = _serialize_pending_actions([_link("Spike A", None)])
        raw[0].pop("target_uuid", None)
        assert _deserialize_pending_actions(raw)[0].target_uuid is None
