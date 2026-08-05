# PyInstaller specification for the stable source-free version-slot launcher.

from pathlib import Path


project_root = Path(SPECPATH).parent
entrypoint = project_root / "src" / "naumi_agent" / "release_launcher_entry.py"

analysis = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "chromadb",
        "jupyter",
        "langchain_core",
        "langgraph",
        "litellm",
        "mcp",
        "notebook",
        "playwright",
        "pytest",
        "textual",
        "tkinter",
    ],
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
    name="naumi-launcher",
)
