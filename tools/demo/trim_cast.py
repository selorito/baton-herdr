"""Cut an asciinema recording where herdr's client says goodbye (`just demo`).

The demo ends by stopping its throwaway herdr server, and the client then prints that the
server shut down. The recording keeps everything before that line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def trim(path: Path) -> None:
    header, *events = path.read_text(encoding="utf-8").splitlines()
    kept = [header]
    for line in events:
        if "shut down" in json.loads(line)[2]:
            break
        kept.append(line)
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


if __name__ == "__main__":
    trim(Path(sys.argv[1]))
