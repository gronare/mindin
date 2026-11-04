from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import anyio
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette

URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


def inner_lifespan(
    app: Starlette, on_exit: Callable[[], None] | None = None
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            async with app.router.lifespan_context(app):
                yield
        finally:
            if on_exit is not None:
                await anyio.to_thread.run_sync(on_exit)

    return lifespan
