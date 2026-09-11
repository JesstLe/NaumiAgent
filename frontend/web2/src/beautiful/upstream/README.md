# Beautiful UI source

Vendored from https://github.com/slev12397/beautiful-ui at
`ff0f74d62d8be9d89bcb735b3632e31a6ccf88dc` under MIT; see `../LICENSE`.

`provenance.json` records original hashes and integration patches. Production
adapters live one directory above and always supply actual application data.
Default upstream samples are reference material, not runtime evidence.

`foundation.css` is the full upstream `app/globals.css`, with explicit Tailwind
source directives. Run `pnpm beautiful:css` in `frontend` to regenerate the
scoped `../upstream.generated.css`. Dev and build run this automatically.
The host uses local Inter / JetBrains Mono fonts and the upstream shadow plugin.
Do not replace the foundation with hand-written approximations or the broken
registry foundation slice.

Thinking, tool chips and selection actions retain the original DOM and motion;
adapters replace simulated completion and edits with actual runtime state or
draft actions. Browser checks cover expand/collapse, content inspection,
selection actions, keyboard dismissal and narrow-screen controls. Public
execution evidence is the only trace displayed.
