"""Strict, side-effect-free provider catalog loading."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlparse

from naumi_agent.model.reasoning import ReasoningEffort, reasoning_effort_values

_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REFERENCE_PATTERN = re.compile(r"^\{(?P<source>env|file):(?P<ref>[^{}]+)\}$")
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "anthropic-api-key",
    "ocp-apim-subscription-key",
}
_SENSITIVE_QUERY_PARAMETERS = {"api_key", "apikey", "key", "token", "access_token"}
_KNOWN_NPM_ADAPTERS = {
    "@ai-sdk/openai-compatible": "openai_chat",
    "@ai-sdk/openai": "openai_responses",
    "@ai-sdk/anthropic": "anthropic_messages",
    "@ai-sdk/google": "google_genai",
    "@ai-sdk/azure": "azure_openai",
}


class ProviderCatalogError(ValueError):
    """Raised when provider catalog input is unsafe or malformed."""


class APIFormat(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GOOGLE_GENAI = "google_genai"
    AZURE_OPENAI = "azure_openai"
    OLLAMA = "ollama"


class AuthType(StrEnum):
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    NONE = "none"


class SecretSource(StrEnum):
    CREDENTIAL = "credential"
    ENV = "env"
    FILE = "file"


@dataclass(frozen=True)
class ProviderAuthSpec:
    type: AuthType
    secret_source: SecretSource | None = None
    secret_ref: str | None = None
    header: str | None = None
    scheme: str | None = None


@dataclass(frozen=True)
class ModelDiscoverySpec:
    enabled: bool = False
    path: str = "/models"
    ttl_seconds: int = 3600


@dataclass(frozen=True)
class ProviderModelSpec:
    id: str
    upstream_id: str
    name: str
    max_context: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_reasoning: bool | None = None
    reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    default_reasoning_effort: ReasoningEffort | None = None
    supports_vision: bool | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    api_format: APIFormat | None
    base_url: str | None
    auth: ProviderAuthSpec
    headers: Mapping[str, str]
    models: Mapping[str, ProviderModelSpec]
    discovery: ModelDiscoverySpec
    whitelist: tuple[str, ...] = ()
    blacklist: tuple[str, ...] = ()
    request_timeout_ms: int | None = None
    chunk_timeout_ms: int | None = None

    def visible_models(self) -> tuple[ProviderModelSpec, ...]:
        allowed = set(self.whitelist)
        blocked = set(self.blacklist)
        return tuple(
            model
            for model in self.models.values()
            if model.id not in blocked and (not allowed or model.id in allowed)
        )


@dataclass(frozen=True)
class ProviderCatalog:
    providers: Mapping[str, ProviderSpec]
    source: str = "<memory>"


def load_provider_catalog(path: str | Path) -> ProviderCatalog:
    """Load one bounded UTF-8 JSON catalog from disk."""
    resolved = Path(path).expanduser().resolve()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ProviderCatalogError(f"无法读取 provider catalog：{resolved}") from exc
    if size > _MAX_CATALOG_BYTES:
        raise ProviderCatalogError("provider catalog 超过 2 MiB 限制。")
    try:
        raw = resolved.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProviderCatalogError(f"provider catalog 必须是 UTF-8 JSON：{resolved}") from exc
    return parse_provider_catalog_json(text, source=str(resolved))


def parse_provider_catalog_json(
    text: str,
    *,
    source: str = "<memory>",
) -> ProviderCatalog:
    """Parse Naumi or OpenCode provider JSON without resolving secrets."""
    if len(text.encode("utf-8")) > _MAX_CATALOG_BYTES:
        raise ProviderCatalogError("provider catalog 超过 2 MiB 限制。")
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except ProviderCatalogError:
        raise
    except json.JSONDecodeError as exc:
        raise ProviderCatalogError(
            f"provider catalog JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）。"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderCatalogError("provider catalog 根节点必须是 JSON object。")
    if "providers" in payload and "provider" in payload:
        raise ProviderCatalogError("provider catalog 不能同时包含 provider 和 providers。")
    if "providers" in payload:
        raw_providers = payload["providers"]
        open_code_shape = False
    elif "provider" in payload:
        raw_providers = payload["provider"]
        open_code_shape = True
    else:
        raise ProviderCatalogError("provider catalog 缺少 providers 或 provider 字段。")
    if not isinstance(raw_providers, dict) or not raw_providers:
        raise ProviderCatalogError("provider catalog 至少需要一个 provider。")

    providers: dict[str, ProviderSpec] = {}
    for raw_id, raw_provider in raw_providers.items():
        provider_id = _normalize_provider_id(raw_id, "provider")
        if provider_id in providers:
            raise ProviderCatalogError(f"provider ID 归一化后重复：{provider_id}")
        providers[provider_id] = _parse_provider(
            provider_id,
            raw_provider,
            open_code_shape=open_code_shape,
        )
    return ProviderCatalog(
        providers=MappingProxyType(providers),
        source=source,
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderCatalogError(f"provider catalog JSON 含重复字段：{key}")
        result[key] = value
    return result


def _parse_provider(
    provider_id: str,
    raw: Any,
    *,
    open_code_shape: bool,
) -> ProviderSpec:
    path = f"provider.{provider_id}"
    data = _mapping(raw, path)
    if open_code_shape:
        return _parse_opencode_provider(provider_id, data, path)
    return _parse_native_provider(provider_id, data, path)


def _parse_native_provider(
    provider_id: str,
    data: dict[str, Any],
    path: str,
) -> ProviderSpec:
    name = _optional_string(data, "name", path) or provider_id
    api_format = _parse_api_format(
        _take_alias(data, ("apiFormat", "api_format"), path, required=True),
        f"{path}.apiFormat",
    )
    base_url = _parse_base_url(
        _take_alias(data, ("baseURL", "base_url"), path, required=True),
        f"{path}.baseURL",
    )
    auth = _parse_native_auth(data.pop("auth", {}), f"{path}.auth")
    headers = _parse_headers(data.pop("headers", {}), f"{path}.headers")
    models = _parse_models(data.pop("models", {}), f"{path}.models")
    discovery = _parse_discovery(data.pop("discovery", {}), f"{path}.discovery")
    whitelist = _parse_filter(data.pop("whitelist", []), f"{path}.whitelist")
    blacklist = _parse_filter(data.pop("blacklist", []), f"{path}.blacklist")
    _reject_unknown(data, path)
    return _build_provider(
        provider_id=provider_id,
        name=name,
        api_format=api_format,
        base_url=base_url,
        auth=auth,
        headers=headers,
        models=models,
        discovery=discovery,
        whitelist=whitelist,
        blacklist=blacklist,
    )


def _parse_opencode_provider(
    provider_id: str,
    data: dict[str, Any],
    path: str,
) -> ProviderSpec:
    name = _optional_string(data, "name", path) or provider_id
    npm = _optional_string(data, "npm", path)
    explicit_format = _take_alias(
        data,
        ("apiFormat", "api_format"),
        path,
        required=False,
    )
    if explicit_format is not None:
        api_format = _parse_api_format(explicit_format, f"{path}.apiFormat")
    elif npm:
        mapped = _KNOWN_NPM_ADAPTERS.get(npm)
        if mapped is None:
            raise ProviderCatalogError(
                f"{path}.npm 不受支持；请显式提供 apiFormat。"
            )
        api_format = APIFormat(mapped)
    else:
        api_format = None

    options = _mapping(data.pop("options", {}), f"{path}.options")
    base_value = _take_alias(
        options,
        ("baseURL", "base_url"),
        f"{path}.options",
        required=False,
    )
    base_url = (
        _parse_base_url(base_value, f"{path}.options.baseURL")
        if base_value is not None
        else None
    )
    auth = _parse_opencode_auth(
        options,
        f"{path}.options",
        api_format=api_format,
    )
    headers = _parse_headers(options.pop("headers", {}), f"{path}.options.headers")
    request_timeout = _optional_positive_int(options, "timeout", f"{path}.options")
    chunk_timeout = _optional_positive_int(options, "chunkTimeout", f"{path}.options")
    _reject_unknown(options, f"{path}.options")

    models = _parse_models(data.pop("models", {}), f"{path}.models")
    discovery = _parse_discovery(data.pop("discovery", {}), f"{path}.discovery")
    whitelist = _parse_filter(data.pop("whitelist", []), f"{path}.whitelist")
    blacklist = _parse_filter(data.pop("blacklist", []), f"{path}.blacklist")
    _reject_unknown(data, path)
    return _build_provider(
        provider_id=provider_id,
        name=name,
        api_format=api_format,
        base_url=base_url,
        auth=auth,
        headers=headers,
        models=models,
        discovery=discovery,
        whitelist=whitelist,
        blacklist=blacklist,
        request_timeout_ms=request_timeout,
        chunk_timeout_ms=chunk_timeout,
    )


def _build_provider(
    *,
    provider_id: str,
    name: str,
    api_format: APIFormat | None,
    base_url: str | None,
    auth: ProviderAuthSpec,
    headers: Mapping[str, str],
    models: Mapping[str, ProviderModelSpec],
    discovery: ModelDiscoverySpec,
    whitelist: tuple[str, ...],
    blacklist: tuple[str, ...],
    request_timeout_ms: int | None = None,
    chunk_timeout_ms: int | None = None,
) -> ProviderSpec:
    overlap = set(whitelist) & set(blacklist)
    if overlap:
        raise ProviderCatalogError(
            f"provider.{provider_id} 的模型同时出现在 whitelist 和 blacklist。"
        )
    if not models and not discovery.enabled:
        raise ProviderCatalogError(
            f"provider.{provider_id} 必须声明至少一个静态模型或启用 discovery。"
        )
    return ProviderSpec(
        id=provider_id,
        name=name,
        api_format=api_format,
        base_url=base_url,
        auth=auth,
        headers=headers,
        models=models,
        discovery=discovery,
        whitelist=whitelist,
        blacklist=blacklist,
        request_timeout_ms=request_timeout_ms,
        chunk_timeout_ms=chunk_timeout_ms,
    )


def _parse_native_auth(raw: Any, path: str) -> ProviderAuthSpec:
    data = _mapping(raw, path)
    raw_type = data.pop("type", None)
    source, ref = _parse_secret_source(data, path)
    if raw_type is None:
        auth_type = AuthType.BEARER if source else AuthType.NONE
    else:
        try:
            auth_type = AuthType(_required_string(raw_type, f"{path}.type"))
        except ValueError as exc:
            raise ProviderCatalogError(f"{path}.type 不受支持。") from exc
    header = _optional_string(data, "header", path)
    scheme = _optional_string(data, "scheme", path)
    _reject_unknown(data, path)
    return _build_auth(auth_type, source, ref, header, scheme, path)


def _parse_opencode_auth(
    options: dict[str, Any],
    path: str,
    *,
    api_format: APIFormat | None,
) -> ProviderAuthSpec:
    source, ref = _parse_secret_source(options, path)
    if source and api_format is APIFormat.ANTHROPIC_MESSAGES:
        auth_type = AuthType.API_KEY_HEADER
        header = "X-API-Key"
    else:
        auth_type = AuthType.BEARER if source else AuthType.NONE
        header = None
    return _build_auth(auth_type, source, ref, header, None, path)


def _parse_secret_source(
    data: dict[str, Any],
    path: str,
) -> tuple[SecretSource | None, str | None]:
    candidates: list[tuple[SecretSource, str]] = []
    credential = _take_alias(
        data,
        ("credentialProvider", "credential_provider"),
        path,
        required=False,
    )
    if credential is not None:
        candidates.append(
            (
                SecretSource.CREDENTIAL,
                _normalize_provider_id(credential, f"{path}.credentialProvider"),
            )
        )
    env = _take_alias(data, ("apiKeyEnv", "env"), path, required=False)
    if env is not None:
        env_name = _required_string(env, f"{path}.env")
        if not _ENV_NAME_PATTERN.fullmatch(env_name):
            raise ProviderCatalogError(f"{path}.env 不是有效的环境变量名。")
        candidates.append((SecretSource.ENV, env_name))
    file_ref = data.pop("file", None)
    if file_ref is not None:
        candidates.append(
            (SecretSource.FILE, _parse_file_reference(file_ref, f"{path}.file"))
        )

    api_key_values = []
    for key in ("apiKey", "api_key"):
        if key in data:
            api_key_values.append((key, data.pop(key)))
    if len(api_key_values) > 1:
        raise ProviderCatalogError(f"{path} 同时声明 apiKey 和 api_key。")
    if api_key_values:
        key, value = api_key_values[0]
        reference = _required_string(value, f"{path}.{key}")
        match = _SECRET_REFERENCE_PATTERN.fullmatch(reference)
        if match is None:
            raise ProviderCatalogError(
                f"{path}.{key} 不允许明文；仅支持 env/file secret reference。"
            )
        source = SecretSource(match.group("source"))
        ref = match.group("ref").strip()
        if source is SecretSource.ENV:
            if not _ENV_NAME_PATTERN.fullmatch(ref):
                raise ProviderCatalogError(f"{path}.{key} 的环境变量名无效。")
        else:
            ref = _parse_file_reference(ref, f"{path}.{key}")
        candidates.append((source, ref))

    if len(candidates) > 1:
        raise ProviderCatalogError(f"{path} 只能声明一个凭据来源。")
    return candidates[0] if candidates else (None, None)


def _build_auth(
    auth_type: AuthType,
    source: SecretSource | None,
    ref: str | None,
    header: str | None,
    scheme: str | None,
    path: str,
) -> ProviderAuthSpec:
    if auth_type is AuthType.NONE and source is not None:
        raise ProviderCatalogError(f"{path} type=none 时不能声明凭据。")
    if auth_type is not AuthType.NONE and source is None:
        raise ProviderCatalogError(f"{path} 缺少凭据引用。")
    if header and header.casefold() in _SENSITIVE_HEADERS and source is None:
        raise ProviderCatalogError(f"{path}.header 缺少安全凭据引用。")
    if auth_type is AuthType.BEARER:
        header = header or "Authorization"
        scheme = scheme or "Bearer"
    elif auth_type is AuthType.API_KEY_HEADER:
        header = header or "X-API-Key"
        scheme = None
    else:
        header = None
        scheme = None
    return ProviderAuthSpec(
        type=auth_type,
        secret_source=source,
        secret_ref=ref,
        header=header,
        scheme=scheme,
    )


def _parse_headers(raw: Any, path: str) -> Mapping[str, str]:
    data = _mapping(raw, path)
    headers: dict[str, str] = {}
    for key, value in data.items():
        name = _required_string(key, f"{path}.name")
        if name.casefold() in _SENSITIVE_HEADERS:
            raise ProviderCatalogError(f"{path} 不允许直接配置敏感认证头。")
        headers[name] = _required_string(value, f"{path}.{name}")
    return MappingProxyType(headers)


def _parse_models(raw: Any, path: str) -> Mapping[str, ProviderModelSpec]:
    data = _mapping(raw, path)
    models: dict[str, ProviderModelSpec] = {}
    for raw_id, raw_model in data.items():
        model_id = _validate_model_id(raw_id, path)
        model_path = f"{path}.{model_id}"
        model = _mapping(raw_model, model_path)
        name = _optional_string(model, "name", model_path) or model_id
        upstream = _take_alias(
            model,
            ("upstreamId", "upstream_id"),
            model_path,
            required=False,
        )
        upstream_id = (
            _validate_model_id(upstream, f"{model_path}.upstreamId")
            if upstream is not None
            else model_id
        )
        limit = _mapping(model.pop("limit", {}), f"{model_path}.limit")
        max_context = _optional_positive_int(limit, "context", f"{model_path}.limit")
        max_output = _optional_positive_int(limit, "output", f"{model_path}.limit")
        _reject_unknown(limit, f"{model_path}.limit")
        capabilities = _mapping(
            model.pop("capabilities", {}), f"{model_path}.capabilities"
        )
        supports_tools = _optional_bool(capabilities, "tools", f"{model_path}.capabilities")
        (
            supports_reasoning,
            reasoning_efforts,
            default_reasoning_effort,
        ) = _parse_reasoning_capability(
            capabilities,
            f"{model_path}.capabilities",
        )
        supports_vision = _optional_bool(
            capabilities, "vision", f"{model_path}.capabilities"
        )
        _reject_unknown(capabilities, f"{model_path}.capabilities")
        modalities = _mapping(model.pop("modalities", {}), f"{model_path}.modalities")
        input_modalities = _parse_string_tuple(
            modalities.pop("input", []), f"{model_path}.modalities.input"
        )
        output_modalities = _parse_string_tuple(
            modalities.pop("output", []), f"{model_path}.modalities.output"
        )
        _reject_unknown(modalities, f"{model_path}.modalities")
        _reject_unknown(model, model_path)
        models[model_id] = ProviderModelSpec(
            id=model_id,
            upstream_id=upstream_id,
            name=name,
            max_context=max_context,
            max_output=max_output,
            supports_tools=supports_tools,
            supports_reasoning=supports_reasoning,
            reasoning_efforts=reasoning_efforts,
            default_reasoning_effort=default_reasoning_effort,
            supports_vision=supports_vision,
            input_modalities=input_modalities,
            output_modalities=output_modalities,
        )
    return MappingProxyType(models)


def _parse_discovery(raw: Any, path: str) -> ModelDiscoverySpec:
    data = _mapping(raw, path)
    enabled = data.pop("enabled", False)
    if not isinstance(enabled, bool):
        raise ProviderCatalogError(f"{path}.enabled 必须是 boolean。")
    endpoint = data.pop("path", "/models")
    endpoint = _required_string(endpoint, f"{path}.path")
    if not endpoint.startswith("/") or "://" in endpoint:
        raise ProviderCatalogError(f"{path}.path 必须是以 / 开头的相对 API 路径。")
    ttl = _take_alias(data, ("ttlSeconds", "ttl_seconds"), path, required=False)
    ttl_seconds = 3600 if ttl is None else _positive_int(ttl, f"{path}.ttlSeconds")
    if not 60 <= ttl_seconds <= 86_400:
        raise ProviderCatalogError(f"{path}.ttlSeconds 必须在 60-86400 之间。")
    _reject_unknown(data, path)
    return ModelDiscoverySpec(enabled=enabled, path=endpoint, ttl_seconds=ttl_seconds)


def _parse_filter(raw: Any, path: str) -> tuple[str, ...]:
    values = _parse_string_tuple(raw, path)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        model_id = _validate_model_id(value, path)
        if model_id in seen:
            raise ProviderCatalogError(f"{path} 含重复模型 ID。")
        seen.add(model_id)
        normalized.append(model_id)
    return tuple(normalized)


def _parse_api_format(raw: Any, path: str) -> APIFormat:
    value = _required_string(raw, path)
    try:
        return APIFormat(value)
    except ValueError as exc:
        supported = "、".join(item.value for item in APIFormat)
        raise ProviderCatalogError(f"{path} 不受支持；可选值：{supported}。") from exc


def _parse_base_url(raw: Any, path: str) -> str:
    value = _required_string(raw, path).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderCatalogError(f"{path} 必须是有效的 HTTP(S) URL。")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderCatalogError(f"{path} 不能包含用户名或密码。")
    if parsed.fragment:
        raise ProviderCatalogError(f"{path} 不能包含 URL fragment。")
    query_names = {
        re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
        for name, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    if query_names & _SENSITIVE_QUERY_PARAMETERS:
        raise ProviderCatalogError(f"{path} 不能包含明文认证参数。")
    return value


def _parse_file_reference(raw: Any, path: str) -> str:
    value = _required_string(raw, path).strip()
    if "\x00" in value:
        raise ProviderCatalogError(f"{path} 含无效路径字符。")
    return value


def _normalize_provider_id(raw: Any, path: str) -> str:
    value = _required_string(raw, path).strip().lower()
    if not _PROVIDER_ID_PATTERN.fullmatch(value):
        raise ProviderCatalogError(f"{path} 不是有效的 provider ID。")
    return value


def _validate_model_id(raw: Any, path: str) -> str:
    value = _required_string(raw, path).strip()
    if len(value) > 256 or any(ord(char) < 32 for char in value):
        raise ProviderCatalogError(f"{path} 含无效模型 ID。")
    return value


def _mapping(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProviderCatalogError(f"{path} 必须是 JSON object。")
    return dict(raw)


def _required_string(raw: Any, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProviderCatalogError(f"{path} 必须是非空字符串。")
    return raw.strip()


def _optional_string(data: dict[str, Any], key: str, path: str) -> str | None:
    if key not in data:
        return None
    return _required_string(data.pop(key), f"{path}.{key}")


def _take_alias(
    data: dict[str, Any],
    aliases: tuple[str, ...],
    path: str,
    *,
    required: bool,
) -> Any:
    present = [alias for alias in aliases if alias in data]
    if len(present) > 1:
        raise ProviderCatalogError(f"{path} 同时声明等价字段：{'、'.join(present)}。")
    if not present:
        if required:
            raise ProviderCatalogError(f"{path} 缺少 {aliases[0]}。")
        return None
    return data.pop(present[0])


def _positive_int(raw: Any, path: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ProviderCatalogError(f"{path} 必须是正整数。")
    return raw


def _optional_positive_int(
    data: dict[str, Any],
    key: str,
    path: str,
) -> int | None:
    if key not in data:
        return None
    return _positive_int(data.pop(key), f"{path}.{key}")


def _optional_bool(data: dict[str, Any], key: str, path: str) -> bool | None:
    if key not in data:
        return None
    value = data.pop(key)
    if not isinstance(value, bool):
        raise ProviderCatalogError(f"{path}.{key} 必须是 boolean。")
    return value


def _parse_reasoning_capability(
    data: dict[str, Any],
    path: str,
) -> tuple[bool | None, tuple[ReasoningEffort, ...], ReasoningEffort | None]:
    key = "reasoning"
    if key not in data:
        return None, (), None
    raw = data.pop(key)
    reasoning_path = f"{path}.{key}"
    if isinstance(raw, bool):
        return raw, (), None
    value = _mapping(raw, reasoning_path)
    if "efforts" not in value:
        raise ProviderCatalogError(f"{reasoning_path}.efforts 为必填字段。")
    raw_efforts = _parse_string_tuple(
        value.pop("efforts"),
        f"{reasoning_path}.efforts",
    )
    if not raw_efforts:
        raise ProviderCatalogError(f"{reasoning_path}.efforts 必须是非空字符串数组。")
    efforts: list[ReasoningEffort] = []
    seen: set[ReasoningEffort] = set()
    for raw_effort in raw_efforts:
        try:
            effort = ReasoningEffort(raw_effort)
        except ValueError as exc:
            raise ProviderCatalogError(
                f"{reasoning_path}.efforts 不受支持；可选值：{reasoning_effort_values()}。"
            ) from exc
        if effort in seen:
            raise ProviderCatalogError(f"{reasoning_path}.efforts 含重复值：{effort.value}。")
        seen.add(effort)
        efforts.append(effort)
    raw_default = _take_alias(
        value,
        ("defaultEffort", "default_effort"),
        reasoning_path,
        required=False,
    )
    default: ReasoningEffort | None = None
    if raw_default is not None:
        default_text = _required_string(raw_default, f"{reasoning_path}.defaultEffort")
        try:
            default = ReasoningEffort(default_text)
        except ValueError as exc:
            raise ProviderCatalogError(
                f"{reasoning_path}.defaultEffort 不受支持；"
                f"可选值：{reasoning_effort_values()}。"
            ) from exc
        if default not in seen:
            raise ProviderCatalogError(
                f"{reasoning_path}.defaultEffort 必须出现在 efforts 中。"
            )
    _reject_unknown(value, reasoning_path)
    return True, tuple(efforts), default


def _parse_string_tuple(raw: Any, path: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ProviderCatalogError(f"{path} 必须是字符串数组。")
    return tuple(_required_string(item, f"{path}[]") for item in raw)


def _reject_unknown(data: dict[str, Any], path: str) -> None:
    if not data:
        return
    field = sorted(data)[0]
    raise ProviderCatalogError(f"{path} 含不支持字段：{field}")
