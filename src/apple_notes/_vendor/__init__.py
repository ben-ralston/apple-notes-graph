"""Vendored single-file clients.

``logger.py`` is a stdlib-only client for an optional, self-hosted log
collector (an HTTP endpoint that accepts JSON events). It is used only through
``apple_notes.unified_log`` and does nothing unless ``LOGGER_URL`` is set.
"""
