"""Public entry points for the ``emote-danmaku`` command."""

from .ass import (  # noqa: F401
    AssDocument,
    Event,
    RenderOptions,
    Style,
    TextMetrics,
    TransformStats,
    read_ass,
    transform,
    write_ass,
)
from .library import EmoteLibrary, LibraryError  # noqa: F401
from .vectorize import TraceError, VectorEmote, trace_image  # noqa: F401

__all__ = [
    "AssDocument",
    "EmoteLibrary",
    "Event",
    "LibraryError",
    "RenderOptions",
    "Style",
    "TextMetrics",
    "TraceError",
    "TransformStats",
    "VectorEmote",
    "read_ass",
    "trace_image",
    "transform",
    "write_ass",
]
__version__ = "1.0.0"
