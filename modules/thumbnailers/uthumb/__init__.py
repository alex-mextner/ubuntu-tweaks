"""uthumb: freedesktop thumbnailers for 3D and web-development files, in one pure-Python package.

GNOME runs every thumbnailer in a bubblewrap sandbox that sees only ``/usr`` (libgnome-desktop,
``gnome-desktop-thumbnail-script.c``): a binary under ``~/.local`` does not exist there. What does exist is the
system ``/usr/bin/python3`` with Pillow and Pygments, so the whole thumbnailer is Python and ships *inside* the
``Exec=`` line of its ``.thumbnailer`` file (``uthumb.bundle``). It reads only the one input file it is given and
writes one PNG.

Entry point: ``uthumb.cli.main(argv)``; the format registry is ``uthumb.registry``.
"""
