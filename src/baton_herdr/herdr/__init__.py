"""Client for the herdr socket API. The only package that knows the herdr protocol."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from baton_herdr.core.config import resolve_herdr_socket_path
from baton_herdr.herdr.host import HerdrPaneHost
from baton_herdr.herdr.transport import HerdrSocket

if TYPE_CHECKING:
    from baton_herdr.core.config import HerdrSettings

__all__ = ["HerdrPaneHost", "HerdrSocket", "connect"]


def connect(settings: HerdrSettings) -> HerdrPaneHost:
    """Pane host for the herdr server selected by ``settings`` and the environment."""
    socket_path = resolve_herdr_socket_path(settings, env=os.environ, home=Path.home())
    return HerdrPaneHost(HerdrSocket(socket_path))
