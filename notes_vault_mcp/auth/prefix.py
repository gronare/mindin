from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette

URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))


def inner_lifespan(app: Starlette) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with app.router.lifespan_context(app):
            yield

    return lifespan
