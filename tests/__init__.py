"""The test package.

Pins ``ubuntu_tweaks.ui`` to "stdout is not a terminal, nothing in the environment asks for color or a
language" for every test: the renderers choose the explained or the compact layout by asking the stream, and a
developer running the suite in a terminal (or with ``CLICOLOR_FORCE`` / ``UBUNTU_TWEAK_LANG`` exported) must get
the same bytes as CI. A test that wants a terminal passes a stream with ``fake_tty = True``
(``tests.test_ui.Tty``) or selects the layout with ``ui.configure(color="always")``.
"""

import os
from typing import TextIO

from ubuntu_tweaks import ui


def _only_fake_ttys(stream: TextIO) -> bool:
    return bool(getattr(stream, "fake_tty", False))


ui._is_tty = _only_fake_ttys
for _name in ("CLICOLOR_FORCE", "NO_COLOR", "UBUNTU_TWEAK_LANG"):
    os.environ.pop(_name, None)
