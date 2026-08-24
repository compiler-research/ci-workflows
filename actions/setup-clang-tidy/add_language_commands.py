#!/usr/bin/env python3
"""Adds compile commands CMake does not emit, so clang-tidy can parse them.

clang-tidy is invoked per file against one compilation database and answers
from that alone. CMake writes an entry per built source, leaving out every
header and every language the project compiles some other way. clang-tidy
then falls back to a neighbouring entry and reads the file in the wrong
language, reporting errors about that rather than about the code.

Generated commands inherit the flags of a C++ entry already in the database,
so they carry the project's own includes, defines and standard rather than a
guess at them.

Usage: add_language_commands.py <build-dir> [<source-dir>] [--force-include H]
"""

import json
import re
import shlex
import sys
from pathlib import Path

HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx")

# Flags whose value is the next argument rather than joined to them. Keeping
# the flag but dropping its value is how an -isystem include path silently
# goes missing.
# What marks a directory as generated rather than written. Any one of them
# is enough; a half-configured tree may not have all.
BUILD_MARKERS = ("CMakeCache.txt", "CMakeFiles", "build.ninja")

VALUE_FLAGS = frozenset({
    "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros",
    "-D", "-U", "-F", "-isysroot", "--sysroot", "-target", "-x", "-arch",
})
CXX_SUFFIXES = (".cpp", ".cc", ".cxx", ".C")

# suffixes: files this language owns by name, if any. content: what marks a
# header as belonging to it, since a name does not. env: the variable that
# has to be set for the language to be considered at all -- a toolchain root
# where one is needed, a bare marker where the language is only a flag.
LANGUAGES = {
    "cuda": {
        "suffixes": (".cu", ".cuh"),
        "content": re.compile(r"__device__|__global__|#\s*include\s*<thrust/"),
        "env": "CUDA_PATH",
        # --cuda-host-only parses device code with no nvcc, no device
        # libraries and no GPU. -Wno-unknown-cuda-version so a toolkit newer
        # than the analysing clang is not itself a finding.
        "flags": lambda root: ["-xcuda", "--cuda-host-only", "-nocudalib",
                               f"--cuda-path={root}",
                               "-Wno-unknown-cuda-version"],
    },
    "openmp": {
        # No suffix of its own: OpenMP is a flag, and the code it hides is
        # in ordinary headers behind _OPENMP.
        "suffixes": (),
        # Only the forms that mean "there is OpenMP code here". A bare
        # _OPENMP would also match `#ifndef _OPENMP`, whose branch -fopenmp
        # would hide rather than reveal.
        "content": re.compile(r"#\s*pragma\s+omp"
                              r"|#\s*include\s*<omp\.h>"
                              r"|#\s*ifdef\s+_OPENMP"
                              r"|defined\s*\(\s*_OPENMP"),
        "env": "CLANG_TIDY_OPENMP",
        # Needs the libomp headers matching the clang-tidy that parses, not
        # the compiler the commands name.
        "flags": lambda _: ["-fopenmp"],
    },
}


def argv_of(entry: dict) -> list[str]:
    return entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])


def base_command(db: list[dict]) -> list[str]:
    """Compiler and flags from a C++ entry, minus its input and output."""
    for entry in db:
        if Path(entry["file"]).suffix not in CXX_SUFFIXES:
            continue
        argv = argv_of(entry)
        kept, i = [argv[0]], 1
        while i < len(argv):
            arg = argv[i]
            if arg == "-o":
                i += 2
            elif arg == "-c":
                i += 1
            elif arg in VALUE_FLAGS and i + 1 < len(argv):
                kept += [arg, argv[i + 1]]
                i += 2
            elif arg.startswith("-"):
                kept.append(arg)
                i += 1
            else:
                i += 1  # a bare path: the input file
        return kept
    raise SystemExit("error: no C++ entry to take flags from")


def iter_sources(source: Path, wanted: tuple[str, ...]):
    """Files under source with one of these suffixes, build trees pruned.

    Walks rather than asking git: a checkout is not always a repository the
    action can query. Build trees are pruned -- the configured one, and any
    stale sibling a developer left behind -- since nothing generated into one
    is the project's own source, and a tree that pulled in a dependency has
    that dependency's headers under it.
    """
    stack = [source]
    while stack:
        directory = stack.pop()
        if directory.name == ".git" or any((directory / m).exists()
                                           for m in BUILD_MARKERS):
            continue
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                stack.append(path)
            elif path.suffix in wanted:
                yield path


def files_for(lang: dict, source: Path) -> list[Path]:
    wanted = (*lang["suffixes"], *HEADER_SUFFIXES)
    found = []
    for path in iter_sources(source, wanted):
        if path.suffix in lang["suffixes"]:
            found.append(path)
        elif lang["content"].search(path.read_text(errors="ignore")):
            found.append(path)
    return found


def needs_prelude(path: Path) -> bool:
    """A file including nothing of the project's own expects to follow one."""
    return not re.search(r'^#\s*include\s*"', path.read_text(errors="ignore"),
                         re.M)


def add_commands(db: list[dict], build: Path, source: Path, environ: dict,
                 prelude: list[str]) -> dict[str, int]:
    base = base_command(db)
    known = {e["file"] for e in db}
    enabled = {n: l for n, l in LANGUAGES.items() if environ.get(l["env"])}

    # A file can belong to more than one language -- a header with both a
    # CUDA and an OpenMP region -- and needs one command carrying both.
    # clang-tidy reads the first entry it finds for a file and ignores the
    # rest, so a command per language would silently drop all but one.
    #
    # One command still means one set of macros, so a header guarding two
    # ways on the same macro has only the branch that command selects
    # analysed. That is why a language is recognised by the code it holds
    # rather than by the macro it guards on: a header merely mentioning
    # __CUDACC__ keeps its ordinary branch, and the CUDA code it reaches for
    # is analysed where that code actually lives.
    per_file: dict[Path, list[str]] = {}
    for name, lang in enabled.items():
        for src in files_for(lang, source):
            if str(src) not in known:
                per_file.setdefault(src, []).append(name)

    added = dict.fromkeys(enabled, 0)
    for src, names in sorted(per_file.items()):
        flags: list[str] = []
        for name in names:
            flags += enabled[name]["flags"](environ[enabled[name]["env"]])
            added[name] += 1
        # A header carries no language in its name, and clang assumes C.
        # A language that sets one (CUDA) has already said so; otherwise say
        # C++, or the file is parsed as C and every include fails.
        if not any(f.startswith("-x") for f in flags):
            flags.insert(0, "-xc++")
        extra = prelude if prelude and needs_prelude(src) else []
        db.append({
            "directory": str(build),
            "file": str(src),
            "arguments": [*base, *flags, *extra, "-c", str(src)],
        })
    return added


def main() -> int:
    import os

    args = sys.argv[1:]
    prelude = []
    if "--force-include" in args:
        i = args.index("--force-include")
        prelude = ["-include", args[i + 1]]
        del args[i:i + 2]

    build = Path(args[0]).resolve()
    source = Path(args[1] if len(args) > 1 else ".").resolve()
    db_path = build / "compile_commands.json"
    if not db_path.exists():
        print(f"error: no {db_path}; did cmake export it?", file=sys.stderr)
        return 1
    db = json.loads(db_path.read_text())

    for name, count in add_commands(db, build, source, os.environ,
                                    prelude).items():
        print(f"{name}: added {count} compile commands")
    db_path.write_text(json.dumps(db, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
