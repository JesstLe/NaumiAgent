#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${NAUMI_CONFIG:-/app/config.yaml}"

if [[ "${NAUMI_BOOTSTRAP:-1}" == "1" ]]; then
  args=(naumi-deploy validate --config "$CONFIG_PATH" --create-dirs)
  if [[ "${NAUMI_REQUIRE_API_KEY:-1}" == "1" ]]; then
    args+=(--require-api-key)
  fi
  "${args[@]}"
fi

exec "$@"
