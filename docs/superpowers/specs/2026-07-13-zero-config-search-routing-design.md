# Zero-Config Search Routing Design

## Goal

A first-time user can search the web without configuring a search-provider API key. Paid or
keyed providers improve quality and latency but are never required for basic search.

## Existing Behavior

`web_search` uses the typed `search.provider_order` route. Brave resolves its token from
`search.brave.api_key_ref` (default `{env:BRAVE_SEARCH_API_KEY}`) and otherwise the router
continues to DuckDuckGo Lite. Provider failures and empty results are returned as human-readable strings.
The orchestrator cannot reliably distinguish missing credentials, provider outage, parsing
failure, and a genuine empty result, so browser automation fallback is an emergent model
choice rather than a deterministic product behavior.

## Search Router

`WebSearchTool` becomes a provider router with one stable result contract. The route is:

1. Use Brave when it is enabled, ordered, and its environment reference resolves a key.
2. If Brave is unavailable, unauthorized, rate-limited, times out, or returns no usable
   results, try the keyless HTTP provider.
3. If keyless HTTP search fails, return a structured fallback request that the engine handles
   by invoking browser search exactly once.
4. Browser search opens a configured public search URL, extracts result links through the
   existing browser runtime, and returns the same normalized result format.

The router prevents fallback loops by carrying attempted provider names in execution-local
state. Browser fallback is read-only, bounded by a timeout, and always closes its session via
the repaired browser lifecycle.

## Provider Contract

Providers return a typed internal result containing:

- `status`: `success`, `empty`, `unavailable`, or `failed`.
- `provider`: stable provider identifier.
- normalized result items (`title`, `url`, `snippet`).
- a safe diagnostic code and message.

User-facing output includes the provider used and, when fallback occurred, one concise
notice. API keys and raw provider responses are never exposed.

## First-Run Experience

Onboarding and doctor report search readiness separately from model readiness:

- `可用（零配置）`: keyless HTTP and browser fallback are available.
- `已增强`: Brave credentials are configured.
- `受限`: neither direct HTTP nor browser runtime is usable.

Search keys are optional in onboarding. Documentation explains the quality/latency benefit
without presenting them as an installation blocker.

## Error Handling

- Authentication and rate-limit failures trigger the next provider without leaking secrets.
- Network and parser failures are bounded and recorded as diagnostics.
- Genuine empty results may try the next provider once, then return a clear empty result.
- Browser fallback failure returns one final error summarizing attempted providers; the model
  is instructed not to retry the same route in the same turn.

## Verification

- Tests cover no key, valid key, invalid key, timeout, rate limit, parser drift, empty result,
  browser success, browser failure, and no fallback loop.
- Onboarding and doctor tests prove search keys remain optional and readiness is accurate.
- A real clean-environment run with search keys removed returns current search results on
  Windows and leaves no browser process or locked conversation.
