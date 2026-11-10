"""Client for the herdr socket API. The only package that knows the herdr protocol."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.core.config import resolve_herdr_socket_path
from baton_herdr.herdr.fallback import (
    HerdrFileExplainer,
    ManifestTitleRules,
    ProcessFallback,
    ProcProcessTable,
    herdr_state_dir,
    running_on_wsl1,
)
from baton_herdr.herdr.host import HerdrPaneHost
from baton_herdr.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from baton_herdr.core.config import HerdrSettings

__all__ = [
    "HerdrPaneHost",
    "HerdrSocket",
    "connect",
    "process_fallback",
    "reads_processes_itself",
]


def reads_processes_itself(settings: HerdrSettings, *, on_wsl1: bool | None = None) -> bool:
    """Whether baton, not herdr, tells which agent runs in a pane (ADR 0022)."""
    if settings.process_detection == "auto":
        return running_on_wsl1() if on_wsl1 is None else on_wsl1
    return settings.process_detection == "baton"


def process_fallback() -> ProcessFallback:
    """baton's own reading of panes, from this machine's processes and herdr's rules."""
    explainer = HerdrFileExplainer(shutil.which("herdr") or "herdr")
    title_rules = ManifestTitleRules(herdr_state_dir(os.environ, Path.home()))
    return ProcessFallback(ProcProcessTable(), explainer, title_rules)


def connect(settings: HerdrSettings) -> HerdrPaneHost:
    """Pane host for the herdr server selected by ``settings`` and the environment."""
    socket_path = resolve_herdr_socket_path(settings, env=os.environ, home=Path.home())
    fallback = process_fallback() if reads_processes_itself(settings) else None
    return HerdrPaneHost(HerdrSocket(socket_path), fallback=fallback)
