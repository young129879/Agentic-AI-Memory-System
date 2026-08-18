"""
HMLR memory service.

Exposes the split-phase recall/ingest API over HTTP so that agents which
bring their own model -- Claude Code, Cursor, CodeBuddy -- can use HMLR's
memory without importing it.

The library API is unchanged; this is a thin layer on top of HMLRClient.
"""

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    """Lazy re-export so importing hmlr.proxy does not require FastAPI."""
    from .server import create_app as _create_app
    return _create_app(*args, **kwargs)
