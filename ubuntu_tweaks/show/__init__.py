"""``ubuntu-tweak show``: a read-only, human-readable overview of all settings of a machine, in Russian.

Accessed via: ``commands/show.py``. Modules: ``collect`` (dconf keys sorted into Settings panels with
values, defaults and status), ``values`` (readable value formats), ``descriptions`` +
``descriptions_ru.toml`` (the hand-written explanations), ``catalog`` (schema summaries), ``system``
(files, timezone/locale/keyboard, packages, modules), ``ssh`` (read-only probes on a remote machine),
``model`` (the data), ``render`` / ``html_page`` (text, Markdown, HTML). Nothing here writes to the machine
inspected; the only file ever written is the report ``--output``.
"""
