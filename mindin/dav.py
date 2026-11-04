from __future__ import annotations

import base64
import contextlib
import mimetypes
import posixpath
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from mindin.backends import MUST_NOT_EXIST, Entry, NotFound, VaultBackend, VersionConflict
from mindin.shared import SharedRefusal, acting_as
from mindin.vault import SubjectVaults

ALLOW = "OPTIONS, PROPFIND, GET, HEAD, PUT, DELETE, MKCOL, MOVE, COPY"
ROOT_ALLOW = "OPTIONS, PROPFIND"
LISTED_NAME = "vault"
READING_METHODS = frozenset({"OPTIONS", "PROPFIND", "GET", "HEAD"})
XML = "application/xml; charset=utf-8"
MAX_BODY_BYTES = 32 * 1024 * 1024
BODY_METHODS = frozenset({"PUT", "MKCOL"})
WRITING_METHODS = frozenset({"PUT", "DELETE", "MKCOL", "MOVE", "COPY"})
Handler = Callable[[Request, VaultBackend, str, str, bytes], Response]


class DavCredentials(Protocol):
    def issue(self, subject: str, label: str = "", account_id: str | None = None) -> str: ...

    def lookup(self, secret: str) -> tuple[str, bool] | None: ...


class BodyTooLarge(Exception):
    pass


async def _bounded_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        raise BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _http_date(moment: datetime) -> str:
    return format_datetime(moment.astimezone(UTC), usegmt=True)


class PreconditionFailed(Exception):
    pass


def etags(header: str) -> list[str]:
    return [tag.strip().removeprefix("W/").strip('"') for tag in header.split(",") if tag.strip()]


def expected_version(request: Request, entry: Entry | None) -> str | None:
    if request.headers.get("if-none-match", "").strip() == "*":
        return MUST_NOT_EXIST
    wanted = etags(request.headers.get("if-match", ""))
    if not wanted:
        return None
    if wanted == ["*"]:
        if entry is None:
            raise PreconditionFailed
        return entry.version
    if len(wanted) == 1:
        return wanted[0]
    if entry is None or entry.version not in wanted:
        raise PreconditionFailed
    return entry.version


def _content_type(key: str) -> str:
    if key.endswith(".md"):
        return "text/markdown; charset=utf-8"
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def _split(path: str) -> tuple[str | None, str] | None:
    name, _, rest = path.lstrip("/").partition("/")
    if name == "":
        return None, ""
    name = unquote(name)
    if name in (".", "..") or "\\" in name:
        return None
    key = _clean(rest)
    return None if key is None else (name, key)


def _clean(raw: str) -> str | None:
    key = unquote(raw).lstrip("/")
    if key == "":
        return key
    segments = key.split("/")
    body = segments[:-1] if key.endswith("/") else segments
    if any(segment in ("", ".", "..") or "\\" in segment for segment in body):
        return None
    return key


def _multistatus(parts: list[str]) -> Response:
    body = '<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>"
    return Response(body, status_code=207, media_type=XML)


class WebDav:
    # The vault answers at mount/<any one name>/ because Remotely Save prefixes every path with its base folder.
    def __init__(
        self, vaults: SubjectVaults, tokens: DavCredentials, mount: str = "/dav", max_body: int = MAX_BODY_BYTES
    ) -> None:
        self.vaults = vaults
        self.tokens = tokens
        self.mount = mount.rstrip("/")
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        response = await self.handle(request)
        await response(scope, receive, send)

    async def handle(self, request: Request) -> Response:
        grant = await anyio.to_thread.run_sync(self._grant, request)
        if grant is None:
            return Response("unauthorized", 401, headers={"WWW-Authenticate": 'Basic realm="vault"'})
        subject, writable = grant
        method = request.method.upper()
        if not writable and method not in READING_METHODS:
            return Response("this key may only read the vault", 403)
        target = self._target(request.scope)
        if target is None:
            return Response("forbidden", 403)
        name, key = target
        if name is None:
            return self._root(request)
        handler = getattr(self, "do_" + method.lower(), None)
        if handler is None:
            return Response("method not allowed", 405, headers={"Allow": ALLOW})
        try:
            body = await _bounded_body(request, self.max_body) if method in BODY_METHODS else b""
        except BodyTooLarge:
            return Response(f"body larger than {self.max_body} bytes", 413)
        base = self.mount + "/" + quote(name, safe="") + "/"
        return await anyio.to_thread.run_sync(self._serve, handler, request, subject, key, base, body)

    def _serve(self, handler: Handler, request: Request, subject: str, key: str, base: str, body: bytes) -> Response:
        backend = self.vaults.for_subject(subject).backend
        actor = getattr(self.tokens, "actor", None)
        try:
            with acting_as(actor(self._secret(request)) if actor else None):
                response = handler(request, backend, key, base, body)
        except NotFound:
            return Response("not found", 404)
        except SharedRefusal as exc:
            return Response(str(exc), 403)
        if request.method.upper() in WRITING_METHODS and response.status_code < 300:
            self.vaults.wrote(subject)
        return response

    def _secret(self, request: Request) -> str:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                return base64.b64decode(value.strip()).decode("utf-8").partition(":")[2]
            except (ValueError, UnicodeDecodeError):
                return ""
        return value.strip() if scheme.lower() == "bearer" else ""

    def _grant(self, request: Request) -> tuple[str, bool] | None:
        secret = self._secret(request)
        return self.tokens.lookup(secret) if secret else None

    def _target(self, scope: Scope) -> tuple[str | None, str] | None:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        if self.mount and path.startswith(self.mount):
            path = path[len(self.mount) :]
        return _split(path)

    def _root(self, request: Request) -> Response:
        method = request.method.upper()
        if method == "OPTIONS":
            return Response(status_code=200, headers={"DAV": "1", "Allow": ROOT_ALLOW, "MS-Author-Via": "DAV"})
        if method != "PROPFIND":
            return Response("method not allowed", 405, headers={"Allow": ROOT_ALLOW})
        root = self.mount + "/"
        parts = [self._response_xml(root, "/", None, True)]
        if request.headers.get("depth", "1") != "0":
            parts.append(self._response_xml(root + LISTED_NAME + "/", LISTED_NAME, None, True))
        return _multistatus(parts)

    # --- resources -------------------------------------------------------------------------------------

    def _lookup(self, backend: VaultBackend, key: str) -> tuple[str, Entry | None] | None:
        if key == "":
            return "dir", None
        base = key.rstrip("/")
        entries = backend.list_all(base)
        if not key.endswith("/"):
            for entry in entries:
                if entry.key == base:
                    return "file", entry
        marker = base + "/"
        for entry in entries:
            if entry.key == marker:
                return "dir", entry
        if any(entry.key.startswith(marker) for entry in entries):
            return "dir", None
        return None

    def _children(self, backend: VaultBackend, folder: str) -> list[tuple[str, Entry | None]]:
        files: dict[str, Entry] = {}
        dirs: dict[str, Entry | None] = {}
        for entry in backend.list_all(folder):
            rest = entry.key[len(folder) :]
            if not rest:
                continue
            first, sep, more = rest.partition("/")
            if sep:
                if not more:
                    dirs[first] = entry
                else:
                    dirs.setdefault(first, None)
            else:
                files[first] = entry
        children: list[tuple[str, Entry | None]] = [(folder + name + "/", dirs[name]) for name in sorted(dirs)]
        children.extend((folder + name, files[name]) for name in sorted(files))
        return children

    def _entry_xml(self, base: str, key: str, entry: Entry | None, is_dir: bool) -> str:
        name = posixpath.basename(key.rstrip("/")) or unquote(base.rstrip("/").rpartition("/")[2])
        return self._response_xml(base + quote(key), name, entry, is_dir, key)

    def _response_xml(self, href: str, name: str, entry: Entry | None, is_dir: bool, key: str = "") -> str:
        href = escape(href)
        name = escape(name)
        modified = _http_date(entry.mtime if entry else _now())
        props = [f"<D:displayname>{name}</D:displayname>", f"<D:getlastmodified>{modified}</D:getlastmodified>"]
        if is_dir:
            props.append("<D:resourcetype><D:collection/></D:resourcetype>")
        else:
            size = entry.size if entry else 0
            version = escape(entry.version) if entry else ""
            props.append("<D:resourcetype/>")
            props.append(f"<D:getcontentlength>{size}</D:getcontentlength>")
            props.append(f"<D:getcontenttype>{escape(_content_type(key))}</D:getcontenttype>")
            props.append(f'<D:getetag>"{version}"</D:getetag>')
        return (
            f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{''.join(props)}</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )

    # --- methods ---------------------------------------------------------------------------------------

    def do_options(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        return Response(status_code=200, headers={"DAV": "1", "Allow": ALLOW, "MS-Author-Via": "DAV"})

    def do_propfind(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        found = self._lookup(backend, key)
        if found is None:
            return Response("not found", 404)
        kind, entry = found
        is_dir = kind == "dir"
        folder = key if key == "" or key.endswith("/") else key + "/"
        parts = [self._entry_xml(base, folder if is_dir else key, entry, is_dir)]
        if is_dir and request.headers.get("depth", "1") != "0":
            parts.extend(
                self._entry_xml(base, child, child_entry, child.endswith("/"))
                for child, child_entry in self._children(backend, folder)
            )
        return _multistatus(parts)

    def do_head(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        response = self.do_get(request, backend, key, base, body)
        return Response(
            status_code=response.status_code, headers=dict(response.headers), media_type=response.media_type
        )

    def do_get(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        found = self._lookup(backend, key)
        if found is None:
            return Response("not found", 404)
        kind, entry = found
        if kind == "dir":
            return Response("collection", 405, headers={"Allow": "OPTIONS, PROPFIND"})
        data, version = backend.get_bytes(key)
        headers = {"ETag": f'"{version}"', "Last-Modified": _http_date(entry.mtime if entry else _now())}
        return Response(data, status_code=200, headers=headers, media_type=_content_type(key))

    def do_put(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        if key == "" or key.endswith("/"):
            return Response("cannot PUT a collection", 405, headers={"Allow": ALLOW})
        found = self._lookup(backend, key)
        existed = found is not None
        content_type = request.headers.get("content-type") or None
        try:
            expected = expected_version(request, found[1] if found else None)
            if expected is None:
                version = backend.put_bytes(key, body, content_type)
            else:
                version = backend.put_checked(key, body, expected, content_type or _content_type(key))
        except (PreconditionFailed, VersionConflict):
            return Response("precondition failed", 412)
        return Response(status_code=204 if existed else 201, headers={"ETag": f'"{version}"'})

    def do_delete(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        if key == "":
            return Response("the vault itself cannot be deleted", 403)
        found = self._lookup(backend, key)
        if found is None:
            return Response("not found", 404)
        kind, _ = found
        if kind == "file":
            backend.delete(key)
            return Response(status_code=204)
        self._delete_tree(backend, key.rstrip("/") + "/")
        return Response(status_code=204)

    def _delete_tree(self, backend: VaultBackend, folder: str) -> None:
        entries = backend.list_all(folder)
        for entry in entries:
            if not entry.is_dir:
                backend.delete(entry.key)
        for entry in sorted((entry for entry in entries if entry.is_dir), key=lambda entry: -len(entry.key)):
            backend.delete(entry.key)
        if not any(entry.key == folder for entry in entries):
            with contextlib.suppress(NotFound):
                backend.delete(folder)

    def do_mkcol(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        if body:
            return Response("unsupported media type", 415)
        if key == "":
            return Response(status_code=201)
        if self._lookup(backend, key) is not None:
            return Response("already exists", 405, headers={"Allow": ALLOW})
        backend.mkdir(key.rstrip("/") + "/")
        return Response(status_code=201)

    def _destination(self, request: Request) -> str | None:
        raw = request.headers.get("destination", "")
        if not raw:
            return None
        target = self._target({"path": urlsplit(raw).path, "root_path": ""})
        if target is None or target[0] is None:
            return None
        return target[1]

    def do_move(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        return self._transfer(request, backend, key, copy=False)

    def do_copy(self, request: Request, backend: VaultBackend, key: str, base: str, body: bytes) -> Response:
        return self._transfer(request, backend, key, copy=True)

    def _transfer(self, request: Request, backend: VaultBackend, key: str, copy: bool) -> Response:
        if key == "":
            return Response("the vault itself cannot be moved or copied", 403)
        dest = self._destination(request)
        if dest is None or dest == "":
            return Response("bad destination", 400)
        found = self._lookup(backend, key)
        if found is None:
            return Response("not found", 404)
        kind, _ = found
        dest_existed = self._lookup(backend, dest) is not None
        if dest_existed and request.headers.get("overwrite", "T").upper() == "F":
            return Response("destination exists", 412)
        if kind == "file":
            self._transfer_file(backend, key, dest.rstrip("/"), copy)
        else:
            source = key.rstrip("/") + "/"
            target = dest.rstrip("/") + "/"
            if target.startswith(source):
                return Response("cannot move a collection into itself", 403)
            if dest_existed:
                self._delete_tree(backend, target)
            self._transfer_tree(backend, source, target, copy)
        return Response(status_code=204 if dest_existed else 201)

    def _transfer_file(self, backend: VaultBackend, src: str, dst: str, copy: bool) -> None:
        if copy:
            data, _ = backend.get_bytes(src)
            backend.put_bytes(dst, data, _content_type(dst))
        else:
            backend.move(src, dst)

    def _transfer_tree(self, backend: VaultBackend, source: str, target: str, copy: bool) -> None:
        entries = backend.list_all(source)
        backend.mkdir(target)
        for entry in entries:
            rest = entry.key[len(source) :]
            if entry.is_dir:
                if rest:
                    backend.mkdir(target + rest)
                continue
            self._transfer_file(backend, entry.key, target + rest, copy)
        if not copy:
            self._delete_tree(backend, source)
