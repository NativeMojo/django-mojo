"""
``_final_reply`` picks the reply for the turn that ends an Assistant run
(maestro item #11025): its own text, else the last step's text (flagged and
not stored again), else a fixed message per stop reason.
"""
from testit import helpers as th
from testit.helpers import assert_eq


STEP = ("The answer is 42.", [], 77)


@th.tier("bug")
@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_final_turn_text_is_the_reply(opts):
    from mojo.apps.assistant.services.agent import _final_reply

    reply = _final_reply([{"type": "text", "text": "Done."}], "end_turn", STEP)
    assert_eq((reply["stored_text"], reply["response"], reply["repeats_step"]),
              ("Done.", "Done.", False), "a final turn with text is stored and returned unchanged")


@th.tier("bug")
@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_empty_final_turn_repeats_step(opts):
    from mojo.apps.assistant.services.agent import _final_reply

    for stop_reason in ("end_turn", "max_tokens", "refusal", "stop_sequence"):
        reply = _final_reply([], stop_reason, STEP)
        assert_eq(reply["response"], STEP[0], f"{stop_reason}: the step text is returned")
        assert_eq(reply["stored_text"], "", f"{stop_reason}: the final Message stays empty")
        assert_eq((reply["repeats_step"], reply["step_message_id"]), (True, 77),
                  f"{stop_reason}: flagged with the step's Message id")


@th.tier("bug")
@th.django_unit_test()
@th.requires_app("mojo.apps.assistant")
def test_no_text_anywhere_gets_fixed_message(opts):
    from mojo.apps.assistant.services.agent import (
        _final_reply, _EMPTY_REPLY_MESSAGES, _EMPTY_REPLY_DEFAULT)

    expected = {
        "max_tokens": _EMPTY_REPLY_MESSAGES["max_tokens"],
        "refusal": _EMPTY_REPLY_MESSAGES["refusal"],
        "end_turn": _EMPTY_REPLY_DEFAULT,
        None: _EMPTY_REPLY_DEFAULT,
    }
    for stop_reason, fixed in expected.items():
        reply = _final_reply([{"type": "text", "text": "   "}], stop_reason, None)
        assert_eq((reply["stored_text"], reply["response"], reply["repeats_step"]),
                  (fixed, fixed, False), f"{stop_reason}: fixed message stored and returned")
