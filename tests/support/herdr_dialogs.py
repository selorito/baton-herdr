"""Recorded dialogs that herdr alone calls idle (ADR 0004 exceptions on herdr's side).

Each must be turned into a blocker by baton-detect; test_adapters checks that, and
test_herdr_state checks that no other dialog is called idle. Add one here on purpose,
after checking that baton-detect catches it.
"""

HERDR_IDLE_DIALOGS = frozenset(
    {
        # Codex's update chooser, under herdr's osc_title_idle (catalog 2026.10.01.1,
        # herdr#4811): a prompt typed into it picks "Update now".
        "codex/blocked_permission/20261007T153746Z",
    }
)
