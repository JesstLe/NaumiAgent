"""Secure runtime mapping for catalog-backed model providers."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from naumi_agent.config.credentials import load_model_api_key
from naumi_agent.model.catalog import (
    APIFormat,
    AuthType,
    ProviderAuthSpec,
    ProviderSpec,
    SecretSource,
)
from naumi_agent.model.targets import ResolvedModelTarget

_MAX_SECRET_FILE_BYTES = 64 * 1024

# Supplying a fixed non-secret value prevents LiteLLM/OpenAI SDK from falling
# back to OPENAI_API_KEY when this provider uses no auth or a custom header.
NO_GLOBAL_API_KEY = "naumi-explicit-no-global-api-key"


class ProviderRuntimeError(ValueError):
    """Raised before transport when selected provider runtime config is unsafe."""


@dataclass(frozen=True)
class ProviderTransport:
    """LiteLLM model name and immutable provider-specific request arguments."""

    model: str
    kwargs: Mapping[str, Any] = field(repr=False)


def build_openai_chat_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport:
    """Map one catalog target to explicit OpenAI-compatible Chat arguments."""
    return _build_openai_transport(
        target,
        catalog_source=catalog_source,
        expected_format=APIFormat.OPENAI_CHAT,
        model_prefix="openai/",
    )


def build_openai_responses_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport:
    """Map one catalog target to LiteLLM's OpenAI Responses bridge."""
    return _build_openai_transport(
        target,
        catalog_source=catalog_source,
        expected_format=APIFormat.OPENAI_RESPONSES,
        model_prefix="openai/responses/",
    )


def build_provider_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
) -> ProviderTransport:
    """Dispatch one catalog target to its explicit provider transport."""
    provider = _require_catalog_provider(target)
    if provider.api_format is APIFormat.OPENAI_CHAT:
        return build_openai_chat_transport(target, catalog_source=catalog_source)
    if provider.api_format is APIFormat.OPENAI_RESPONSES:
        return build_openai_responses_transport(target, catalog_source=catalog_source)
    if provider.api_format is None:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 缺少 apiFormat，无法选择请求适配器。'
        )
    raise ProviderRuntimeError(
        f'provider "{provider.id}" 的 {provider.api_format.value} 适配器尚未实现。'
    )


def _build_openai_transport(
    target: ResolvedModelTarget,
    *,
    catalog_source: str,
    expected_format: APIFormat,
    model_prefix: str,
) -> ProviderTransport:
    """Build shared explicit kwargs for one OpenAI-family transport."""
    provider = _require_catalog_provider(target)
    _validate_provider_format(provider, expected_format=expected_format)

    static_headers = dict(provider.headers)
    _assert_no_auth_header_conflict(provider, static_headers)
    api_key, auth_header = _resolve_auth(
        provider,
        catalog_source=catalog_source,
    )
    if auth_header is not None:
        header_name, header_value = auth_header
        if any(name.casefold() == header_name.casefold() for name in static_headers):
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的静态 header 与认证头冲突。'
            )
        static_headers[header_name] = header_value

    kwargs: dict[str, Any] = {
        "api_base": provider.base_url,
        "api_key": api_key,
        "extra_headers": MappingProxyType(static_headers),
    }
    if provider.request_timeout_ms is not None:
        if provider.request_timeout_ms <= 0:
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的 request timeout 必须大于 0。'
            )
        kwargs["timeout"] = provider.request_timeout_ms / 1000

    return ProviderTransport(
        model=f"{model_prefix}{target.upstream_model}",
        kwargs=MappingProxyType(kwargs),
    )


def _require_catalog_provider(target: ResolvedModelTarget) -> ProviderSpec:
    if target.source != "catalog":
        raise ProviderRuntimeError("provider runtime 只接受 provider catalog 模型。")
    provider = target.provider
    if provider is None:
        raise ProviderRuntimeError("catalog 模型缺少 provider 运行时信息。")
    return provider


def _validate_provider_format(
    provider: ProviderSpec,
    *,
    expected_format: APIFormat,
) -> None:
    if provider.api_format is None:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 缺少 apiFormat，无法选择请求适配器。'
        )
    if provider.api_format is not expected_format:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的 {provider.api_format.value} '
            f"不能由 {expected_format.value} 适配器处理。"
        )
    if not provider.base_url:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 缺少 baseURL，无法发起模型请求。'
        )


def _assert_no_auth_header_conflict(
    provider: ProviderSpec,
    static_headers: Mapping[str, str],
) -> None:
    if provider.auth.type is AuthType.NONE:
        return
    auth_header = provider.auth.header
    if not auth_header:
        auth_header = (
            "Authorization"
            if provider.auth.type is AuthType.BEARER
            else "X-API-Key"
        )
    if any(name.casefold() == auth_header.casefold() for name in static_headers):
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的静态 header 与认证头冲突。'
        )


def _resolve_auth(
    provider: ProviderSpec,
    *,
    catalog_source: str,
) -> tuple[str, tuple[str, str] | None]:
    auth = provider.auth
    if auth.type is AuthType.NONE:
        if any(
            value is not None
            for value in (auth.secret_source, auth.secret_ref, auth.header, auth.scheme)
        ):
            raise _invalid_auth(provider)
        return NO_GLOBAL_API_KEY, None

    if auth.secret_source is None or not auth.secret_ref:
        raise _invalid_auth(provider)
    secret = _resolve_secret(
        provider,
        auth,
        catalog_source=catalog_source,
    )

    if auth.type is AuthType.BEARER:
        header = auth.header or "Authorization"
        scheme = auth.scheme or "Bearer"
        if header.casefold() == "authorization" and scheme.casefold() == "bearer":
            return secret, None
        return NO_GLOBAL_API_KEY, (header, f"{scheme} {secret}")
    if auth.type is AuthType.API_KEY_HEADER:
        header = auth.header or "X-API-Key"
        return NO_GLOBAL_API_KEY, (header, secret)
    raise _invalid_auth(provider)


def _resolve_secret(
    provider: ProviderSpec,
    auth: ProviderAuthSpec,
    *,
    catalog_source: str,
) -> str:
    source = auth.secret_source
    reference = auth.secret_ref
    if source is None or not reference:
        raise _invalid_auth(provider)

    if source is SecretSource.CREDENTIAL:
        try:
            secret = load_model_api_key(
                provider=reference,
                fallback_to_legacy=False,
            )
        except Exception:
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 无法读取 credential 凭据。'
            ) from None
        return _require_secret_value(provider, source, secret)

    if source is SecretSource.ENV:
        return _require_secret_value(provider, source, os.environ.get(reference))

    if source is SecretSource.FILE:
        return _read_secret_file(
            provider,
            reference,
            catalog_source=catalog_source,
        )

    raise _invalid_auth(provider)


def _require_secret_value(
    provider: ProviderSpec,
    source: SecretSource,
    value: str | None,
) -> str:
    stripped = value.strip() if value is not None else ""
    if not stripped:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的 {source.value} 凭据缺失或为空。'
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in stripped):
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的 {source.value} 凭据包含控制字符。'
        )
    return stripped


def _read_secret_file(
    provider: ProviderSpec,
    reference: str,
    *,
    catalog_source: str,
) -> str:
    try:
        configured = Path(reference).expanduser()
    except (RuntimeError, ValueError):
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的 file 凭据路径无效。'
        ) from None

    try:
        if configured.is_absolute():
            resolved = configured.resolve()
        else:
            catalog_path = Path(catalog_source).expanduser()
            if not catalog_path.is_absolute():
                raise ProviderRuntimeError(
                    f'provider "{provider.id}" 的内存 catalog 不能使用相对 file 凭据。'
                )
            base_dir = catalog_path.parent.resolve()
            resolved = (base_dir / configured).resolve()
            if not resolved.is_relative_to(base_dir):
                raise ProviderRuntimeError(
                    f'provider "{provider.id}" 的 file 凭据路径超出 catalog 目录。'
                )
    except ProviderRuntimeError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 的 file 凭据路径无效。'
        ) from None

    return _read_bounded_utf8_file(provider, resolved)


def _read_bounded_utf8_file(provider: ProviderSpec, path: Path) -> str:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的 file 凭据必须是普通文件：{path}'
            )
        if file_stat.st_size > _MAX_SECRET_FILE_BYTES:
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的 file 凭据超过 64 KiB：{path}'
            )
        raw = _read_descriptor(descriptor)
        if len(raw) > _MAX_SECRET_FILE_BYTES:
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的 file 凭据超过 64 KiB：{path}'
            )
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderRuntimeError(
                f'provider "{provider.id}" 的 file 凭据必须是 UTF-8：{path}'
            ) from None
    except ProviderRuntimeError:
        raise
    except OSError:
        raise ProviderRuntimeError(
            f'provider "{provider.id}" 无法读取 file 凭据：{path}'
        ) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return _require_secret_value(provider, SecretSource.FILE, value)


def _invalid_auth(provider: ProviderSpec) -> ProviderRuntimeError:
    return ProviderRuntimeError(f'provider "{provider.id}" 的认证配置无效。')


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_SECRET_FILE_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
