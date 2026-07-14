# PyInstaller specification for the source-free Naumi backend distribution.

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)


project_root = Path(SPECPATH).parent
entrypoint = project_root / "src" / "naumi_agent" / "packaging_entry.py"

datas = []
binaries = []
hiddenimports = []


def include_runtime_submodule(name):
    excluded = (
        "chromadb.cli",
        "chromadb.server",
        "litellm.proxy",
    )
    return (
        ".tests" not in name
        and ".test" not in name
        and not name.startswith(excluded)
    )

# These packages load providers/plugins dynamically; their Python modules are stored
# inside PyInstaller archives, while non-code runtime data remains in _internal.
for package in (
    "chromadb",
    "keyring",
    "langchain_core",
    "langgraph",
    "litellm",
    "mcp",
    "playwright",
    "textual",
    "tiktoken_ext",
):
    hiddenimports += collect_submodules(
        package,
        filter=include_runtime_submodule,
    )
    datas += collect_data_files(
        package,
        excludes=["**/tests/**", "**/test/**", "**/*.ipynb"],
    )
    binaries += collect_dynamic_libs(package)

for distribution in ("naumi-agent", "litellm", "chromadb", "playwright"):
    try:
        datas += copy_metadata(distribution, recursive=True)
    except Exception:
        pass

analysis = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["IPython", "jupyter", "notebook", "pytest", "tkinter"],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="naumi",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="naumi",
)
