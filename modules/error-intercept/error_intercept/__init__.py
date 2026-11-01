"""error-intercept: apport crash reports and shell error windows become events for the agent subscribed to errors.

Accessed via: ``modules/error-intercept/error-intercept`` (the CLI, also what the user systemd units run) and
``modules/error-intercept/installer`` (the module installer). Spec: docs/specs/error-intercept.md.

Modules: ``paths`` (where everything lives), ``crash_report`` (parse an apport ``.crash`` header), ``sanitize``
(what may leave the report), ``classify`` (severity, kind, dedup key), ``apport_source`` (scan /var/crash),
``spool`` (events the shell extension dropped), ``publisher`` (``agent-sub publish`` with a durable retry
queue), ``audit`` (append-only JSONL), ``archive`` (gzip a report out of the crash directory), ``cli``.
"""
