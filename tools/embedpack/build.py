#!/usr/bin/env python
"""embedpack — build a portable Windows app from a Python project.

Output layout (folder distribution, no installer, no registry):

    <Out>/<Name>/
      <Name>.exe              launcher shell (prebuilt C#, copies of templates/)
      python.exe / pythonw.exe
      pythonXY.dll, python3.dll, *.pyd, vcruntime*.dll, libcrypto/libssl ...
      pythonXY.zip            CPython stdlib, shipped as .pyc by the official embeddable
      pythonXY._pth           sys.path isolation: zip, ., site-packages, app
      site-packages/          third-party deps: .pyc + native .pyd + data files
      app/                    your project: .pyc + data files, entry at __main__.pyc

Requirements of the build machine:
  * the interpreter running this script is the same Python the app targets —
    .pyc bytecode is version-specific (magic number per X.Y);
  * dependencies listed in requirements.txt are already installed in that
    interpreter (e.g. `uv pip install -r requirements.txt`); they are copied
    into the output by reading each distribution's RECORD, never executed;
  * internet access (first run) to download the official embeddable zip;
  * git when the source root sits in a work tree: the project copy follows
    `git ls-files`, i.e. the project's own .gitignore (--include overrides);
    without git, only the built-in floor excludes apply (--exclude trims).

Usage:
    python build.py                       # reads ./embedpack.toml when present
    python build.py --config app.toml --gui
    python build.py --name MyApp --entry src/main.py \
        --requirements requirements.txt --out dist [--optimize 2]

CLI flags override embedpack.toml; --config/--embed-zip/--embed-url/--cache
are build-machine concerns and stay on the command line.
"""

from __future__ import annotations

import argparse
import compileall
import fnmatch
import importlib.metadata as im
import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER_CONSOLE = os.path.join(HERE, "templates", "launcher_console.exe")
LAUNCHER_GUI = os.path.join(HERE, "templates", "launcher_gui.exe")

EMBED_URL = "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-{arch}.zip"

FLOOR_EXCLUDES = ["__pycache__", "*.pyc", "*.egg-info", ".git*"]

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]{0,59}$")

REQ_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")

CONFIG_NAME = "embedpack.toml"

CONFIG_SCHEMA: dict[str, type] = {
    "app.name": str,
    "app.entry": str,
    "app.src-root": str,
    "app.gui": bool,
    "dist.out": str,
    "dist.packages": list,
    "dist.requirements": str,
    "dist.include": list,
    "dist.exclude": list,
    "dist.optimize": int,
    "dist.keep-py": bool,
    "dist.tkinter": bool,
}


def canonical(name: str) -> str:
    """dist name spelling -> canonical form (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def as_list(value: str | list[str] | None) -> list[str]:
    """CLI comma-separated string or config list -> flat list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return [str(p) for p in value]


def info(msg: str) -> None:
    print(f"  [+] {msg}")


def die(msg: str) -> None:
    print(f"  [x] {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(path: str | None) -> dict[str, object]:
    """Load the embedpack project description into a flat {section.key: value}.

    An explicitly given path must exist; without one, ./embedpack.toml is
    picked up when present. Every key is validated against CONFIG_SCHEMA —
    closed inventory, typos die instead of silently no-oping."""
    if path is not None:
        cfg_path = os.path.abspath(path)
        if not os.path.isfile(cfg_path):
            die("config not found: " + cfg_path)
    else:
        cfg_path = os.path.join(os.getcwd(), CONFIG_NAME)
        if not os.path.isfile(cfg_path):
            return {}
    try:
        with open(cfg_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"{cfg_path}: invalid TOML: {e}")
    allowed: dict[str, set[str]] = {}
    for dotted in CONFIG_SCHEMA:
        section, key = dotted.split(".", 1)
        allowed.setdefault(section, set()).add(key)
    flat: dict[str, object] = {}
    for section, table in data.items():
        if section not in allowed:
            die(f"{cfg_path}: unknown section or top-level key [{section}] "
                f"(expected one of {', '.join(sorted(allowed))})")
        if not isinstance(table, dict):
            die(f"{cfg_path}: [{section}] must be a table")
        for key, value in table.items():
            if key not in allowed[section]:
                die(f"{cfg_path}: unknown key {section}.{key} "
                    f"(expected one of {', '.join(sorted(allowed[section]))})")
            flat[f"{section}.{key}"] = value
    for dotted, value in flat.items():
        typ = CONFIG_SCHEMA[dotted]
        if typ is list:
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            want = "a list of strings"
        elif typ is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
            want = "an integer"
        else:
            ok = isinstance(value, typ)
            want = "a " + typ.__name__
        if not ok:
            die(f"{cfg_path}: {dotted} must be {want}")
    return flat


def contains(tree: str, path: str) -> bool:
    """True when path equals tree or lies inside it."""
    try:
        return os.path.commonpath([tree, path]) == tree
    except ValueError:
        return False


def validated(path: str, root: str) -> str:
    """Assert path (after normalization) stays inside root; return the real path."""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if not contains(root_real, path_real):
        raise ValueError("path escapes " + root + ": " + path_real)
    return path_real


def safe_wipe_dir(path: str) -> None:
    """Remove an output directory after refusing dangerous locations.

    Windows keeps transient handles on freshly-written files (indexer,
    antivirus), so deletion retries briefly before giving up."""
    target = os.path.realpath(path)
    protected = {os.path.realpath(os.getcwd()),
                 os.path.realpath(os.path.expanduser("~")),
                 os.path.realpath(HERE)}
    if target in protected or os.path.dirname(target) == target:
        die("refusing to delete a protected directory: " + target)
    for attempt in range(6):
        try:
            shutil.rmtree(target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(1.0)


def download_embed(version: str, arch: str, cache_dir: str, url_template: str) -> str:
    """Return a cached or freshly downloaded embeddable zip path."""
    os.makedirs(cache_dir, exist_ok=True)
    zip_name = "python-" + version + "-embed-" + arch + ".zip"
    zip_path = os.path.join(cache_dir, zip_name)
    if os.path.isfile(zip_path):
        info(f"embeddable {version} ({arch}) cached: {zip_path}")
        return zip_path
    url = url_template.format(ver=version, arch=arch)
    info("downloading " + url)
    tmp = zip_path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.replace(tmp, zip_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return zip_path


def safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """Extract rejecting members that would escape dest (zip-slip guard)."""
    for member in zf.namelist():
        validated(os.path.join(dest, member), dest)
    zf.extractall(dest)


def parse_requirements(path: str) -> list[str]:
    """Extract distribution names from a requirements.txt (versions ignored).

    Requirements gated off by their environment markers (evaluated for this
    interpreter) are skipped, mirroring transitive-dependency handling."""
    names: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-"):
                die("unsupported requirements line: " + line)
            if not REQ_NAME_RE.match(line):
                die("cannot parse requirement: " + line)
            dep = requirement_name(line)
            if dep is None:
                info("requirement skipped by environment marker: " + line)
                continue
            names.append(dep)
    return names


def requirement_name(req: str) -> str | None:
    """Name of a PEP 508 requirement; None when the requirement is inactive.

    packaging, when importable, decides via environment markers; the fallback
    extracts the name prefix and drops extra-gated requirements."""
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        if "extra ==" in req or "extra==" in req:
            return None
        m = REQ_NAME_RE.match(req)
        return m.group(0) if m else None
    try:
        r = Requirement(req)
    except InvalidRequirement:
        m = REQ_NAME_RE.match(req)
        return m.group(0) if m else None
    if r.marker is not None and not r.marker.evaluate({"extra": ""}):
        return None
    return r.name


def copy_distributions(names: list[str], dst: str) -> None:
    """Copy installed distributions (plus their transitive requirements) into dst.

    Files are taken from each distribution's RECORD; paths containing '..'
    (venv scripts etc.) and bytecode caches are skipped, so only importable
    package content plus data files plus dist-info land in dst.
    """
    top_level = {canonical(n) for n in names}
    done: dict[str, im.Distribution] = {}
    pending: deque[str] = deque(sorted(top_level))
    while pending:
        name = pending.popleft()
        if name in done:
            continue
        try:
            dist = im.distribution(name)
        except im.PackageNotFoundError:
            if name in top_level:
                die(f"dependency not installed in the build interpreter: {name}\n"
                    f"    install it first, e.g.  uv pip install {name}")
            info(f"transitive dep not installed, skipped: {name}")
            continue
        done[name] = dist
        for req in dist.requires or []:
            dep = requirement_name(req)
            if dep:
                pending.append(canonical(dep))
    copied: set[str] = set()
    for dist in done.values():
        files = dist.files or []
        if not files:
            die(f"no RECORD for {dist.metadata['Name']}; reinstall it")
        for f in files:
            rel = str(f).replace("/", os.sep)
            parts = rel.split(os.sep)
            if ".." in parts or "__pycache__" in parts:
                continue
            if rel.endswith((".pyc", ".pyo")):
                continue
            if rel in copied:
                continue
            src = dist.locate_file(f)
            if not os.path.isfile(src):
                continue
            target = validated(os.path.join(dst, rel), dst)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            copied.add(rel)
    info(f"deps copied: {len(done)} distributions, {len(copied)} files")


def excluded(rel: str, patterns: list[str]) -> bool:
    """True when any path segment of rel matches one of the fnmatch patterns."""
    return any(fnmatch.fnmatch(part, p) for part in rel.split(os.sep) for p in patterns)


def git_enum(root: str, extra_args: list[str]) -> list[str] | None:
    """git ls-files output for root (NUL-separated names); None on any failure."""
    cmd = ["git", "-C", root, "ls-files", "-z", *extra_args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", "surrogateescape")
    return [name for name in raw.split("\0") if name]


def copy_project_tree(src_root: str, dst: str, extra_excludes: list[str],
                      force_includes: list[str]) -> None:
    """Copy the project source tree into dst.

    Inside a git work tree the copy set comes from git ls-files (tracked plus
    untracked, ignores honored), so the project's own .gitignore decides what
    stays out of the distribution; force_includes override it. Outside git,
    everything ships except the floor patterns (--exclude trims further)."""
    floor = FLOOR_EXCLUDES + extra_excludes

    def forced(rel: str) -> bool:
        posix = rel.replace(os.sep, "/")
        return any(fnmatch.fnmatch(posix, p) for p in force_includes)

    def take(rel: str) -> bool:
        src = os.path.join(src_root, rel)
        if not os.path.isfile(src):
            return False
        target = validated(os.path.join(dst, rel), dst)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(src, target)
        return True

    files = git_enum(src_root, ["--cached", "--others", "--exclude-standard"])
    if files is not None:
        ignored = git_enum(src_root, ["--others", "--ignored", "--exclude-standard"])
        if ignored is None:
            die("git ls-files failed while listing ignored files")
        picked = files + [f for f in ignored if forced(f.replace("/", os.sep))]
        copied = 0
        for name in picked:
            rel = name.replace("/", os.sep)
            if excluded(rel, floor) and not forced(rel):
                continue
            if take(rel):
                copied += 1
        info(f"app source: {copied} files via git ls-files (.gitignore honored)")
        return

    info("app source: no git work tree, copying everything past the floor")

    def ignore(_dirpath: str, names: list[str]) -> list[str]:
        return [n for n in names
                if not forced(os.path.relpath(os.path.join(_dirpath, n), src_root))
                and any(fnmatch.fnmatch(n, p) for p in floor)]

    shutil.copytree(src_root, dst, ignore=ignore)


def copy_tkinter(out_dir: str, optimize: int, keep_py: bool) -> None:
    """Bundle tkinter from the build interpreter into the embeddable output.

    Tcl 9 builds embed the script library inside the DLLs (zipfs), so
    _tkinter + tcl/tk/zlib/tommath DLLs suffice. Classic 8.6 installs keep
    an external tcl/ tree, which is copied alongside and activated via a
    site-packages .pth exporting TCL_LIBRARY/TK_LIBRARY.
    """
    import tkinter
    pkg_src = os.path.dirname(os.path.abspath(tkinter.__file__))
    pkg_dst = os.path.join(out_dir, "tkinter")
    shutil.copytree(pkg_src, pkg_dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    spec = importlib.util.find_spec("_tkinter")
    if spec is None or spec.origin is None:
        die("tkinter is not installed in the build interpreter")
    pyd_src = spec.origin
    dll_dir = os.path.dirname(pyd_src)
    patterns = ("tcl*", "tk*", "*tommath*", "zlib1*", "icu*")
    for name in os.listdir(dll_dir):
        if name.lower().endswith(".dll") and any(
                fnmatch.fnmatch(name.lower(), p) for p in patterns):
            shutil.copy2(os.path.join(dll_dir, name), out_dir)
    shutil.copy2(pyd_src, out_dir)

    lib = tkinter.Tcl().eval("info library")
    if not lib.startswith("//zipfs"):
        tcl_src = os.path.dirname(os.path.normpath(lib))
        tcl_dst = os.path.join(out_dir, "tcl")
        shutil.copytree(tcl_src, tcl_dst, ignore=shutil.ignore_patterns("__pycache__"))
        env = ("import os,sys;d=os.path.dirname(sys.executable);"
               "os.environ.setdefault('TCL_LIBRARY',os.path.join(d,'tcl','tcl8.6'));"
               "os.environ.setdefault('TK_LIBRARY',os.path.join(d,'tcl','tk8.6'))")
        sp_dir = os.path.join(out_dir, "site-packages")
        os.makedirs(sp_dir, exist_ok=True)
        Path(os.path.join(sp_dir, "tkinter_env.pth")).write_text(env + "\n", encoding="utf-8")

    compile_tree(pkg_dst, optimize)
    if not keep_py:
        n = strip_sources(pkg_dst)
        info(f"tkinter bundled: {n} .py stripped, tcl {os.path.basename(lib)}")


def compile_tree(root: str, optimize: int) -> None:
    """Compile every .py under root into a legacy-layout .pyc beside the source."""
    src = os.path.abspath(root)
    ok = compileall.compile_dir(src, quiet=1, legacy=True, force=True,
                                optimize=optimize, workers=0)
    if not ok:
        die("compilation failed under " + src)


def strip_sources(root: str) -> int:
    """Delete .py sources and stale __pycache__ dirs; return removed file count."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if d == "__pycache__":
                shutil.rmtree(validated(os.path.join(dirpath, d), root))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if fn.endswith(".py"):
                os.remove(validated(os.path.join(dirpath, fn), root))
                removed += 1
    return removed


def strip_shadowed_pyc(root: str) -> int:
    """Remove .pyc files whose module also ships as a sibling .pyd; return count."""
    removed = 0
    for dirpath, _, filenames in os.walk(root):
        pyds = [fn for fn in filenames if fn.endswith(".pyd")]
        for fn in filenames:
            if fn.endswith(".pyc") and any(
                    pyd.startswith(fn[:-4] + ".") for pyd in pyds):
                os.remove(validated(os.path.join(dirpath, fn), root))
                removed += 1
    return removed


def dir_stats(root: str) -> tuple[int, int, int]:
    """Return (file_count, total_bytes, leftover_.py_count) inside root."""
    n_files = n_bytes = n_py = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            n_files += 1
            n_bytes += os.path.getsize(os.path.join(dirpath, fn))
            n_py += fn.endswith(".py")
    return n_files, n_bytes, n_py


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="embedpack",
        description="Build: Windows embeddable Python + dep .pyc + app .pyc + .exe launcher")
    ap.add_argument("--config",
                    help=f"project description file (default: ./{CONFIG_NAME} when present)")
    ap.add_argument("--name", help="app name, becomes the exe file name (config: app.name)")
    ap.add_argument("--entry",
                    help="entry .py of the project, runs as __main__ (config: app.entry)")
    ap.add_argument("--requirements",
                    help="requirements.txt of deps installed in this interpreter")
    ap.add_argument("--packages",
                    help="comma-separated dist names (alternative to --requirements)")
    ap.add_argument("--src-root",
                    help="copy this source root into app/ (default: entry's directory)")
    ap.add_argument("--out", help="output directory (config: dist.out; default: dist)")
    ap.add_argument("--gui", action="store_true", default=None,
                    help="windowed launcher (pythonw.exe, no console)")
    ap.add_argument("--console", action="store_true", default=None,
                    help="force the console launcher when the config sets gui")
    ap.add_argument("--optimize", type=int, default=None, choices=(0, 1, 2),
                    help="bytecode optimize level (config: dist.optimize; "
                         "default 0; 2 strips asserts+docstrings)")
    ap.add_argument("--keep-py", action="store_true", default=None,
                    help="keep .py sources (debugging)")
    ap.add_argument("--with-tkinter", action="store_true", default=None,
                    help="bundle tkinter/tcl runtime from this interpreter")
    ap.add_argument("--embed-zip", help="use a local embeddable zip instead of downloading")
    ap.add_argument("--embed-url", default=EMBED_URL,
                    help="embeddable zip URL template with {ver} and {arch} "
                         "placeholders (default: python.org)")
    ap.add_argument("--exclude",
                    help="comma-separated glob patterns skipped at any depth when "
                         "copying the project, added to the built-in list")
    ap.add_argument("--include",
                    help="comma-separated globs force-copied even when gitignored "
                         "or excluded (matched against paths relative to the "
                         "source root)")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"),
                    help="embeddable download cache dir")
    args = ap.parse_args()
    cfg = load_config(args.config)

    def pick(flag: object, key: str, default: object = None) -> object:
        return flag if flag is not None else cfg.get(key, default)

    name_in = pick(args.name, "app.name")
    if not name_in:
        die(f"app name missing: pass --name or set app.name in {CONFIG_NAME}")
    app_name = os.path.basename(str(name_in))
    if not NAME_RE.match(app_name):
        print(f"  [x] invalid app name: {name_in!r}", file=sys.stderr)
        raise SystemExit(1)
    gui = (args.gui is True or cfg.get("app.gui") is True) and args.console is not True
    launcher_src = LAUNCHER_GUI if gui else LAUNCHER_CONSOLE
    if not os.path.isfile(launcher_src):
        die("prebuilt launcher missing: " + launcher_src)

    ver = platform.python_version()
    tag = "".join(str(v) for v in sys.version_info[:2])
    machine = platform.machine().lower()
    arch = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64",
            "aarch64": "arm64"}.get(machine)
    if not arch:
        die(f"unsupported build machine: {machine}")

    entry_in = pick(args.entry, "app.entry")
    if not entry_in:
        die(f"entry missing: pass --entry or set app.entry in {CONFIG_NAME}")
    entry = os.path.realpath(str(entry_in))
    if not entry.endswith(".py") or not os.path.isfile(entry):
        die(f"entry must be an existing .py file: {entry}")
    src_root = pick(args.src_root, "app.src-root")
    project_root = os.path.realpath(str(src_root)) if src_root else os.path.dirname(entry)
    if not contains(project_root, entry):
        die(f"entry is outside the source root: {entry} not inside {project_root}")

    req_in = pick(args.requirements, "dist.requirements")
    pkg_in = pick(args.packages, "dist.packages")
    if req_in and pkg_in:
        die("--requirements and --packages are mutually exclusive")
    dep_names: list[str] = []
    if req_in:
        req_path = os.path.abspath(str(req_in))
        if not os.path.isfile(req_path):
            die("requirements file not found: " + req_path)
        dep_names = parse_requirements(req_path)
    else:
        dep_names = as_list(pkg_in)

    optimize = pick(args.optimize, "dist.optimize", 0)
    if optimize not in (0, 1, 2):
        die(f"dist.optimize must be 0, 1 or 2: {optimize!r}")
    keep_py = bool(pick(args.keep_py, "dist.keep-py", False))
    with_tk = bool(pick(args.with_tkinter, "dist.tkinter", False))

    out_root = os.path.abspath(str(pick(args.out, "dist.out", "dist")))
    out_dir = os.path.realpath(os.path.join(out_root, app_name))
    if contains(out_dir, project_root) or contains(project_root, out_dir):
        die("output directory overlaps the project source: " + out_dir)
    if os.path.exists(out_dir):
        safe_wipe_dir(out_dir)
    os.makedirs(out_dir)
    app_dir = os.path.join(out_dir, "app")
    sp_dir = os.path.join(out_dir, "site-packages")

    print(f"== embedpack: {app_name} (python {ver}, {arch}, "
          f"{'gui' if gui else 'console'}) ==")

    # 1. embeddable runtime
    if args.embed_zip:
        embed_zip = os.path.abspath(args.embed_zip)
    else:
        embed_zip = download_embed(ver, arch, args.cache, args.embed_url)
    with zipfile.ZipFile(embed_zip) as zf:
        names = zf.namelist()
        if f"python{tag}.dll" not in names or f"python{tag}.zip" not in names:
            die(f"{embed_zip} is not a python {ver} embeddable package")
        safe_extract(zf, out_dir)
    info(f"runtime extracted from {os.path.basename(embed_zip)}")

    # 2. dependencies from this interpreter
    if dep_names:
        os.makedirs(sp_dir, exist_ok=True)
        copy_distributions(dep_names, sp_dir)
        compile_tree(sp_dir, optimize)
        if not keep_py:
            n = strip_sources(sp_dir)
            info(f"site-packages: {n} .py stripped, bytecode only")
        shadowed = strip_shadowed_pyc(sp_dir)
        if shadowed:
            info(f"site-packages: {shadowed} shadowed .pyc removed")
    else:
        info("no dependencies: stdlib-only app")

    if with_tk:
        copy_tkinter(out_dir, optimize, keep_py)

    # 3. project -> app/, entry -> __main__.pyc
    extra_excludes = as_list(pick(args.exclude, "dist.exclude"))
    force_includes = as_list(pick(args.include, "dist.include"))

    copy_project_tree(project_root, app_dir, extra_excludes, force_includes)
    entry_rel_src = os.path.relpath(entry, project_root)
    if not os.path.isfile(validated(os.path.join(app_dir, entry_rel_src), app_dir)):
        die(f"entry missing from the copied source set: {entry_rel_src}"
            " (gitignored or excluded; see --include)")
    compile_tree(app_dir, optimize)
    if not keep_py:
        strip_sources(app_dir)
    shadowed = strip_shadowed_pyc(app_dir)
    if shadowed:
        info(f"app: {shadowed} shadowed .pyc removed")
    entry_rel = os.path.relpath(entry, project_root)[:-3] + ".pyc"
    entry_pyc_path = validated(os.path.join(app_dir, entry_rel), app_dir)
    if not os.path.isfile(entry_pyc_path):
        die(f"entry did not compile to app/{entry_rel}")
    final_entry = os.path.join(app_dir, "__main__.pyc")
    if entry_rel.replace(os.sep, "/") != "__main__.pyc":
        if os.path.exists(final_entry):
            die("app/__main__.pyc already exists; entry collision")
        os.replace(entry_pyc_path, validated(final_entry, app_dir))
    info("app compiled: entry app/__main__.pyc")

    # 4. sys.path isolation
    pth_name = f"python{tag}._pth"
    Path(out_dir).joinpath(pth_name).write_text(
        f"python{tag}.zip\n.\nsite-packages\napp\nimport site\n", encoding="utf-8")
    info(f"wrote {pth_name}")

    # 5. launcher shell
    exe_file = app_name + ".exe"
    exe_path = os.path.realpath(os.path.join(out_dir, exe_file))
    shutil.copy2(launcher_src, exe_path)
    info(f"launcher installed: {os.path.basename(exe_path)} "
         f"({'pythonw' if gui else 'python'})")

    # 6. summary
    n_files, n_bytes, n_py = dir_stats(out_dir)
    print(f"== done: {n_files} files, {n_bytes / 1e6:.1f} MB, .py remaining: {n_py} ==")
    print(f"    run: {exe_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
