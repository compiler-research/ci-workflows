#!/usr/bin/env bash
# Computes a deterministic cache key for a recipe build.
#
# Args:  RECIPE  VERSION  OS  ARCH
# Usage: compute-key.sh llvm-asan 22 ubuntu-24.04 x86_64 [recipe_root]
#
# `recipe_root` defaults to "recipes/" relative to cwd (i.e. expects to
# be invoked from the ci-workflows repo root). Pass an explicit path
# when invoked from elsewhere.
#
# Output: a single line `key=<recipe>-<version>-<os>-<arch>-<short-hash>`
# suitable for appending to $GITHUB_OUTPUT.
set -euo pipefail

RECIPE="$1"
VERSION="$2"
OS="$3"
ARCH="$4"
RECIPE_ROOT="${5:-recipes}"

dir="${RECIPE_ROOT}/${RECIPE}"
if [[ ! -d "$dir" ]]; then
  echo "compute-key.sh: recipe directory not found: $dir" >&2
  exit 1
fi

# Hash inputs that *should* invalidate when changed:
#   - recipe.yaml  (declarative metadata)
#   - build.sh     (imperative build)
#   - patches/**   (any local patches applied to the source)
#   - the literal version/os/arch tuple
#
# What we deliberately do NOT include:
#   - runner image SHA — bumps shouldn't invalidate every cell. Image
#     details land in the manifest for forensics.
#   - timestamps — keys must be reproducible.
hash=$(
  {
    sha256sum "$dir/recipe.yaml"
    sha256sum "$dir/build.sh"
    if [[ -d "$dir/patches" ]]; then
      ( cd "$dir/patches" && \
        find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum )
    fi
    printf 'V=%s OS=%s ARCH=%s\n' "$VERSION" "$OS" "$ARCH"
  } | sha256sum | awk '{print $1}'
)

short="${hash:0:16}"
echo "key=${RECIPE}-${VERSION}-${OS}-${ARCH}-${short}"
