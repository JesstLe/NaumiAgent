const API_FORMAT_LABELS = Object.freeze({
  openai_chat: "OpenAI Chat",
  openai_responses: "OpenAI Responses",
  anthropic_messages: "Anthropic Messages",
  google_genai: "Google GenAI",
  azure_openai: "Azure OpenAI",
  ollama: "Ollama",
  legacy: "兼容模式",
});

export function formatApiFormat(value) {
  const format = String(value || "").trim();
  if (!format) return "未解析";
  return API_FORMAT_LABELS[format] ?? format;
}

export function formatProviderIdentity(status = {}) {
  const provider = String(status.provider || "").trim() || "未解析";
  return `${provider}/${formatApiFormat(status.api_format)}`;
}

export function upstreamModelMapping(status = {}) {
  const model = String(status.model || "").trim();
  const upstream = String(status.upstream_model || "").trim();
  return upstream && upstream !== model ? upstream : "";
}
