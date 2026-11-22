set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

default:
    @just --list

lint:
    uv run ruff check src tests tools
    uv run ruff format --check src tests

fmt:
    uv run ruff check --fix src tests
    uv run ruff format src tests

type:
    uv run mypy

test:
    uv run pytest

pyd:
    uv run python -c "from setuptools import setup; from Cython.Build import cythonize; setup(ext_modules=cythonize('src/pyapp/core.py', language_level=3), script_args=['build_ext', '--inplace', '--build-temp', 'build/pyd-tmp'])"
    uv run python -c "import os, shutil; shutil.rmtree('build', ignore_errors=True); p = 'src/pyapp/core.c'; os.path.isfile(p) and os.remove(p)"

dist *flags:
    uv run python tools/embedpack/build.py {{flags}}

dist-gui: (dist "--gui")

clean:
    uv run python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('dist', 'build', '.pytest_cache', '.mypy_cache', '.ruff_cache')]"
