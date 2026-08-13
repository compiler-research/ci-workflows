"""Rewrite a recipe's recorded cmake invocation for replay in a devshell.

The manifest stores the exact cmake command the producer ran (see
llvm_build.record_cmake_args). Replaying it verbatim is what keeps a devshell's
compile commands identical to the producer's, which is the whole basis of the
sibling-ccache pre-warm -- ccache hashes the full command line, so any drift
turns hits into misses.

Three arguments are producer-specific and have to be rewritten: the install
prefix and the source and build directories, all of which name paths on a
filesystem the consumer does not have.

Recipes record two different shapes. LLVM's build.py runs cmake from inside the
build directory and passes a relative `../llvm`, because its CMakeLists lives in
an llvm/ subdirectory rather than at the tree root. Every other recipe passes an
absolute `-S <dir> -B <dir>`. Handling only the first is how the devshell came
to work exclusively for LLVM.
"""

from __future__ import annotations

import posixpath
from typing import List, Sequence

#: cmake flags whose value is a producer path we must replace. Each accepts the
#: value either as the next argument (`-S dir`) or joined (`-Sdir`).
_PATH_FLAGS = ("-S", "-B")


def rewrite(args: Sequence[str], *, prefix: str, src: str,
            build: str) -> List[str]:
    """Return `args` with producer paths replaced by the local ones.

    `args` is the manifest's cmake_args, including the leading "cmake".
    `src` is the source tree root; LLVM's `../llvm` resolves to `src/llvm`.
    """
    replacement = {"-S": src, "-B": build}
    # LLVM keeps its CMakeLists one level down; other projects are at the root.
    #
    # posixpath, not os.path: every path here is a path *inside the devshell
    # container*, which is always Linux. os.path would join with whatever
    # separator the host running this module happens to use, producing
    # "/local/src\llvm" when the unit tests run on a Windows CI leg.
    llvm_src = posixpath.join(src, "llvm")

    out: List[str] = []
    pending: str | None = None
    for arg in args:
        if pending is not None:
            out.append(pending)
            pending = None
            continue
        if arg == "cmake":
            continue
        if arg.startswith("-DCMAKE_INSTALL_PREFIX="):
            out.append(f"-DCMAKE_INSTALL_PREFIX={prefix}")
            continue
        if arg in _PATH_FLAGS:
            out.append(arg)
            pending = replacement[arg]
            continue
        if arg[:2] in _PATH_FLAGS and len(arg) > 2:
            out.append(f"{arg[:2]}{replacement[arg[:2]]}")
            continue
        if arg == "../llvm":
            out.append(llvm_src)
            continue
        out.append(arg)
    return out
