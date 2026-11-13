from __future__ import annotations

from mindin.backends import VaultBackend
from mindin.backends.local import LocalBackend
from mindin.config import NO_BACKEND, ConfigError, local_root, s3_settings, vault_id


def make_backend() -> tuple[VaultBackend, str]:
    root = local_root()
    if root is not None:
        return LocalBackend(root), vault_id(str(root))

    settings = s3_settings()
    if settings is not None:
        from mindin.backends.s3 import S3Backend  # boto3 loads only for an S3 vault, keeping hook startup fast

        seed = settings["endpoint"] + settings["bucket"] + settings["prefix"]
        return S3Backend(**settings), vault_id(seed)

    raise ConfigError(NO_BACKEND)


def make_subject_backend(subject: str, prefix: str = "") -> tuple[VaultBackend, str]:
    # One vault per subject under the configured storage: a sub-folder of the local root, or a deeper S3 prefix.
    root = local_root()
    if root is not None:
        path = root.joinpath(*(part for part in (prefix.strip("/"), subject) if part))
        return LocalBackend(path), vault_id(str(path))

    settings = s3_settings()
    if settings is not None:
        from mindin.backends.s3 import S3Backend  # boto3 loads only for an S3 vault, keeping hook startup fast

        combined = "/".join(part for part in (settings["prefix"].strip("/"), prefix.strip("/"), subject) if part)
        seed = settings["endpoint"] + settings["bucket"] + combined
        return S3Backend(**{**settings, "prefix": combined}), vault_id(seed)

    raise ConfigError(NO_BACKEND)
