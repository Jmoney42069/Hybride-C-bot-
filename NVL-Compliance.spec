# -*- mode: python ; coding: utf-8 -*-
# NVL-Compliance PyInstaller spec — --onedir mode voor maximale betrouwbaarheid
import os
import glob
from PyInstaller.utils.hooks import collect_all, collect_submodules

PYTHON_DIR = r"C:\Users\User\AppData\Local\Programs\Python\Python313"
DLLS_DIR = os.path.join(PYTHON_DIR, "DLLs")

# ── ALLE .pyd en .dll uit Python's DLLs map ──
extra_binaries = []
for ext in ("*.pyd", "*.dll"):
    for f in glob.glob(os.path.join(DLLS_DIR, ext)):
        name = os.path.basename(f)
        if "_test" in name:
            continue
        extra_binaries.append((f, "."))

# ── Tkinter data bestanden (voor installer GUI) ──
TKINTER_DIR = os.path.join(PYTHON_DIR, "tcl")
if os.path.isdir(TKINTER_DIR):
    for sub in os.listdir(TKINTER_DIR):
        full = os.path.join(TKINTER_DIR, sub)
        if os.path.isdir(full):
            extra_binaries.append((full, os.path.join("tcl", sub)))

# ── Hidden imports ──
all_hiddenimports = [
    # C-extensies + stdlib
    "unicodedata", "encodings", "codecs",
    "encodings.utf_8", "encodings.ascii", "encodings.latin_1",
    "encodings.cp1252", "encodings.idna",
    "idna", "idna.core", "idna.codec", "idna.package_data",
    "charset_normalizer",
    "ssl", "_ssl", "socket", "_socket",
    "http", "http.client", "http.server",
    "urllib3", "urllib3.util", "urllib3.util.ssl_",
    "queue", "_queue", "decimal", "_decimal",
    "ctypes", "_ctypes", "hashlib", "_hashlib",
    "bz2", "_bz2", "lzma", "_lzma",
    "uuid", "_uuid", "asyncio", "_asyncio",
    "select", "multiprocessing", "_multiprocessing",
    "_overlapped", "pyexpat", "xml.parsers.expat",
    "_elementtree", "xml.etree.ElementTree",
    "sqlite3", "_sqlite3",
    "email", "email.mime", "email.mime.text", "email.mime.multipart",
    "smtplib", "json", "tempfile", "webbrowser", "threading",
    # Tkinter (voor installer GUI)
    "tkinter", "_tkinter", "tkinter.ttk", "tkinter.constants",
    # Win32 (voor snelkoppelingen)
    "win32com", "win32com.client", "win32api", "pythoncom", "pywintypes",
    "winreg",
    # Onze packages
    "numpy", "sounddevice", "soundfile",
    "flask", "flask_cors",
    "requests", "groq", "cerebras", "cerebras.cloud", "cerebras.cloud.sdk",
    "dotenv",
    # Vaak gemist
    "pkg_resources", "packaging", "packaging.version",
    "packaging.specifiers", "packaging.requirements",
    "certifi", "cffi",
    "pydantic", "pydantic_core", "pydantic_core._pydantic_core",
    "httpx", "httpcore", "anyio", "anyio._backends",
    "anyio._backends._asyncio", "sniffio",
    "h11", "h2", "hpack", "hyperframe",
    "typing_extensions", "typing_inspection",
    "annotated_types",
    "markupsafe", "jinja2",
    "werkzeug", "click", "blinker", "itsdangerous",
]

# collect_submodules voor alle packages
for pkg in [
    "requests", "urllib3", "idna", "charset_normalizer", "certifi",
    "flask", "flask_cors", "werkzeug", "jinja2", "markupsafe",
    "click", "blinker", "itsdangerous",
    "groq", "cerebras",
    "httpx", "httpcore", "anyio", "sniffio", "h11",
    "pydantic", "pydantic_core",
    "numpy", "sounddevice", "soundfile",
    "dotenv", "encodings",
    "win32com", "win32com.client",
    "tkinter",
]:
    try:
        all_hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

all_hiddenimports = list(set(all_hiddenimports))

# ── collect_all voor packages met data files ──
extra_datas = []
for pkg in ["certifi", "pydantic", "pydantic_core", "sounddevice", "soundfile"]:
    try:
        d, b, h = collect_all(pkg)
        extra_datas += d
        extra_binaries += b
        all_hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=extra_binaries,
    datas=[
        ("compliance-ui.html", "."),
    ] + extra_datas,
    hiddenimports=all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "PIL", "IPython"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ── --onedir mode: exe + COLLECT folder ──
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NVL-Compliance",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NVL-Compliance",
)
