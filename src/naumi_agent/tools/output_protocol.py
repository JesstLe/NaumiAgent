"""Rich output contract shared by generation and publishing tools."""
# ruff: noqa: E501

RICH_OUTPUT_PROTOCOL = r'''
- Web2 renders Markdown/GFM, fenced code, $inline LaTeX$, $$display LaTeX$$, and image links. Use these directly when useful.
- For interactive output, return a fenced `naumi` block containing valid JSON with version:1, type, title, optional source (actual data provenance), and the type-specific fields below. It persists as assistant message text; CLI/TUI can read the JSON. Do not wrap the block in another code fence.
- metrics: items:[{label,value,unit?,note?}], up to 12 cards. table: columns:[{key,label}], rows:[{key:value}], up to 20 columns and 2000 rows. Tables support search, sort, pagination, CSV export and computed numeric statistics.
- chart: x:"category_field", series:[{key,label}], rows:[{category_field:"label",value_field:12}], style:"bar"|"line"|"scatter". Use numeric series/null, at most 6 series and 300 rows; aggregate larger datasets with real tools first. Users can toggle series and switch to the source table.
- image: url:"actual accessible image URL", caption?:"description". Never invent an image URL; publish an actual generated file when a file publishing tool is available.
- html: html:"self-contained HTML with inline CSS/vanilla JS", height:420. The user starts the isolated interactive component. No external libraries/network/API calls, forms, parent access, credentials, popups or imports; use embedded data and inline SVG/canvas for simulations, calculators and visual explanations. Component state resets on reload; never claim it is saved server-side. Do not use a markdown `html` block for interactive output, as ordinary code blocks stay source code.
- tabs: tabs:[{label,content:{type,title,...}}] groups up to 8 components; only the outer object requires version:1. Nest at most 3 levels. Prefer one concise component over a giant dashboard.
- Example: ```naumi\n{"version":1,"type":"table","title":"Results","columns":[{"key":"value","label":"Value"}],"rows":[{"value":12}]}\n```
- Ground metrics, charts and analysis in actual input/tool data. Label hypothetical examples explicitly. Do not expose hidden reasoning, API keys or local secrets in component code/data.
'''
