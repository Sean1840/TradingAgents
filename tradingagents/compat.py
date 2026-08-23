"""Runtime compatibility patches for upstream dependency quirks.

``patch_httpx2_brotli`` — httpx2 2.12.0's ``BrotliDecoder.decode`` calls
``self._decompress(data, output_buffer_limit=...)``. When the installed brotli
package is google's ``Brotli`` (whose ``Decompressor`` only exposes
``process(data)``), that keyword argument raises ``TypeError: process() takes
no keyword arguments`` — breaking every response that arrives brotli-compressed
(e.g. DeepSeek's chat completions). The patched ``decode`` feeds data without
the keyword, which works for both the ``brotli`` and ``brotlicffi`` backends.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def patch_httpx2_brotli() -> None:
    try:
        from httpx2 import _decoders
    except Exception:  # noqa: BLE001 — httpx2 absent; nothing to patch
        return

    if getattr(_decoders.BrotliDecoder, "_dsh_patched", False):
        return

    def decode(self, data: bytes):  # noqa: ANN001
        if not data:
            return
        self.seen_data = True
        try:
            out = self._decompress(data)
            while out:
                yield out
                out = self._decompress(b"")
        except Exception as exc:  # noqa: BLE001
            raise _decoders.DecodingError(str(exc)) from exc

    _decoders.BrotliDecoder.decode = decode
    _decoders.BrotliDecoder._dsh_patched = True
    logger.debug("patched httpx2 BrotliDecoder for google-brotli process() signature")
