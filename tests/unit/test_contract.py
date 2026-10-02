from __future__ import annotations

from baton_herdr.core.contract import END_CONTRACT, EndMark, EndStatus, one_line, parse_end


def test_the_last_mark_counts_and_the_paragraph_above_it_is_kept() -> None:
    screen = (
        "⏺ Earlier turn.\n"
        "  [[BATON:END status=done]]\n"
        "\n"
        "> next prompt\n"
        "\n"
        "⏺ Should power(0, 0) return 1 or raise?\n"
        "  I can do either.\n"
        "  [[BATON:END status=question]]\n"
        "╭──────────╮\n"
        "│ >        │\n"
        "╰──────────╯\n"
    )
    assert parse_end(screen) == EndMark(
        EndStatus.QUESTION, "Should power(0, 0) return 1 or raise? I can do either."
    )


def test_no_mark_or_the_echoed_contract_is_no_signal() -> None:
    assert parse_end("⏺ Done, all tests pass.\n") is None
    # The prompt is echoed on screen; its placeholder must never read as a mark.
    assert parse_end(f"> Add a test.\n\n{END_CONTRACT}\n") is None
    assert parse_end("[[BATON:END status=finished]]") is None


def test_long_context_is_cut() -> None:
    mark = parse_end("x" * 2000 + "\n[[BATON:END status=blocked]]")
    assert mark is not None
    assert mark.status is EndStatus.BLOCKED
    assert len(mark.text) == 500


def test_prompts_are_sent_as_one_line() -> None:
    assert one_line("Fix the bug.\n\n  Then run the tests.\r\nReport back.\n") == (
        "Fix the bug. Then run the tests. Report back."
    )
    assert "\n" not in END_CONTRACT
