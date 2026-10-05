"""Every local import in webapp/build must name a module that exists here.

This is the test that was missing on 2026-10-05. fetch_signals.py was deleted
from the public capture repo for good reasons, but fetch_prek_myschools.py
imports canon_addr from it at module level, so that fetcher could no longer be
imported at all. MySchools is a required fetcher, so every capture run aborted
on it and the repository that exists to collect public records collected
nothing for three hours.

Nothing caught it. pyflakes runs in CI and passes, because
`from fetch_signals import canon_addr` is perfectly valid syntax -- pyflakes
checks undefined NAMES, not whether a module resolves. The canaries probe live
government sources, not our own import graph. And the two repos hold different
subsets of these files, so an import that resolves in one can dangle in the
other, which is precisely the case that bit.

Deliberately STATIC: it parses the import statements rather than executing
them. Several of these modules do real work at import time (fetch_prek_myschools
builds district tables, build_data is enormous), so importing them to prove they
import would be slow and would hit the network. Resolving the names is enough
to catch a deleted or renamed module, which is the whole failure mode.
"""
import ast
import os
import sys

BUILD = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Third-party packages the build legitimately depends on. Anything not here and
# not in the standard library is treated as a local module and must exist, so a
# genuinely new dependency belongs on this list and a typo'd or deleted module
# does not. Kept as an explicit allowlist rather than probing with find_spec,
# because the optional ones below are deliberately NOT installed in CI and a
# probe would make this test pass or fail on the runner's package set.
#
#   requests, openpyxl  installed by both repos' workflows
#   pyflakes, pytest    test tooling
#   anthropic           generate_narratives.py, imported lazily inside the
#                       function; the build no-ops without ANTHROPIC_API_KEY
#   pyodbc              fetch_schools_nysed.py, needs the MS Access ODBC
#                       driver and is run by hand on Windows, never in CI
THIRD_PARTY = {"requests", "openpyxl", "pyflakes", "bs4", "pytest",
               "anthropic", "pyodbc"}


def _local_modules():
    return {f[:-3] for f in os.listdir(BUILD) if f.endswith(".py")}


def _imported_modules(path):
    """(module, lineno) for every absolute import in one file."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name.split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module.split(".")[0], node.lineno))
    return out


def test_every_local_import_resolves():
    local = _local_modules()
    stdlib = set(sys.stdlib_module_names)
    dangling = []
    for fn in sorted(os.listdir(BUILD)):
        if not fn.endswith(".py"):
            continue
        for mod, line in _imported_modules(os.path.join(BUILD, fn)):
            if mod in stdlib or mod in THIRD_PARTY or mod in local:
                continue
            dangling.append(f"{fn}:{line} imports '{mod}', which is not a module in this repo")
    assert not dangling, "unresolvable imports:\n  " + "\n  ".join(dangling)


def test_every_build_module_parses():
    # A syntax error in a fetcher only shows up when that fetcher runs, which
    # for the yearly workbook readers can be months later.
    broken = []
    for fn in sorted(os.listdir(BUILD)):
        if not fn.endswith(".py"):
            continue
        try:
            with open(os.path.join(BUILD, fn), encoding="utf-8") as fh:
                ast.parse(fh.read(), filename=fn)
        except SyntaxError as e:
            broken.append(f"{fn}: {e}")
    assert not broken, "files that do not parse:\n  " + "\n  ".join(broken)
