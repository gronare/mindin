from __future__ import annotations

import json
import struct
import sys
from typing import BinaryIO

HEADER = struct.Struct(">II")
LENGTH = struct.Struct(">I")
BATCH_SIZE = 32


def read_exactly(stream: BinaryIO, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            return None
        data += part
    return data


def read_message(stream: BinaryIO) -> bytes | None:
    header = read_exactly(stream, LENGTH.size)
    if header is None:
        return None
    return read_exactly(stream, LENGTH.unpack(header)[0])


def write_message(stream: BinaryIO, payload: bytes) -> None:
    stream.write(LENGTH.pack(len(payload)) + payload)
    stream.flush()


def serve(folder: str, model: str, requests: BinaryIO, replies: BinaryIO) -> None:
    import numpy
    from fastembed import TextEmbedding

    embedding = TextEmbedding(model, cache_dir=folder)
    while (request := read_message(requests)) is not None:
        try:
            rows = numpy.asarray(list(embedding.embed(json.loads(request), batch_size=BATCH_SIZE)), dtype=numpy.float32)
            write_message(replies, HEADER.pack(*rows.shape) + rows.tobytes())
        except Exception as exc:
            write_message(replies, HEADER.pack(0, 0) + str(exc).encode("utf-8"))


def main() -> None:
    folder, model = sys.argv[1], sys.argv[2]
    replies = sys.stdout.buffer
    # Replies own stdout; anything a library prints must not land in them.
    sys.stdout = sys.stderr
    serve(folder, model, sys.stdin.buffer, replies)


if __name__ == "__main__":
    main()
