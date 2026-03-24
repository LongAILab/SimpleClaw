"""Test bootstrap helpers."""

from __future__ import annotations

import sys
import types


try:
    import tiktoken  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("tiktoken")

    class Encoding:
        def encode(self, text: str, *args, **kwargs) -> list[int]:
            return list((text or "").encode("utf-8"))

    def get_encoding(_: str) -> Encoding:
        return Encoding()

    module.Encoding = Encoding
    module.get_encoding = get_encoding
    module.encoding_for_model = get_encoding
    sys.modules["tiktoken"] = module


try:
    import loguru  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("loguru")

    class _Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    module.logger = _Logger()
    sys.modules["loguru"] = module
