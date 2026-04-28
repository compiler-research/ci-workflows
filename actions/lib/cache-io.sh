#!/usr/bin/env bash
# Scheme-aware probe / download / upload primitives for the recipe
# cache. Source this from an action or from bin/recipe-cache.
#
# Supported schemes:
#   file:///abs/path  — local directory acting as the cache backend
#                       (developer machines, act runs, NFS mounts)
#   https://...       — remote URL; reads use curl. Writes are only
#                       supported when the URL points at a github.com
#                       Releases asset path, in which case `gh release
#                       upload` is used. Any other host is read-only.
#   http://...        — same as https://, for lab webservers without TLS.
#
# Functions assume the caller has already exported an OS-appropriate
# zstd, tar, curl, and (for github uploads) gh.

# Note: do not enable nounset here — actions sourcing this file may
# leave optional env vars unset and rely on the lib to handle them.

# resolve_cache_base [EXPLICIT]
#   Emits the effective cache base URL on stdout. Precedence:
#     1. EXPLICIT argument (action input or CLI flag), when non-empty
#     2. RECIPE_CACHE_BASE env var, when non-empty
#     3. Baked-in default — the compiler-research/ci-workflows
#        Releases cache.
resolve_cache_base() {
  local explicit="${1:-}"
  if [[ -n "$explicit" ]]; then
    printf '%s\n' "$explicit"
    return
  fi
  if [[ -n "${RECIPE_CACHE_BASE:-}" ]]; then
    printf '%s\n' "$RECIPE_CACHE_BASE"
    return
  fi
  printf 'https://github.com/compiler-research/ci-workflows/releases/download/cache/\n'
}

# _strip_trailing_slash URL  →  URL without trailing slash on stdout.
_strip_trailing_slash() {
  local u="$1"
  printf '%s\n' "${u%/}"
}

# cache_probe BASE KEY  → exit 0 if asset exists, 1 if missing,
#                        2 on unsupported scheme.
cache_probe() {
  local base key
  base="$(_strip_trailing_slash "$1")"
  key="$2"
  case "$base" in
    file://*)
      local dir="${base#file://}"
      [[ -f "$dir/${key}.tar.zst" ]]
      ;;
    https://*|http://*)
      curl -fsLI "${base}/${key}.tar.zst" >/dev/null 2>&1
      ;;
    *)
      echo "cache_probe: unsupported scheme: $base" >&2
      return 2
      ;;
  esac
}

# cache_download BASE KEY OUT_DIR
#   Fetches the asset and extracts it into OUT_DIR. The recipe's
#   tarball root (e.g. llvm-project/) lands directly under OUT_DIR.
cache_download() {
  local base key out
  base="$(_strip_trailing_slash "$1")"
  key="$2"
  out="$3"
  mkdir -p "$out"
  case "$base" in
    file://*)
      local dir="${base#file://}"
      zstd -d < "$dir/${key}.tar.zst" | tar -xf - -C "$out"
      ;;
    https://*|http://*)
      curl -fsL "${base}/${key}.tar.zst" | zstd -d | tar -xf - -C "$out"
      ;;
    *)
      echo "cache_download: unsupported scheme: $base" >&2
      return 2
      ;;
  esac
}

# cache_upload BASE KEY ASSET MANIFEST
#   Stores the asset and manifest at the cache backend.
#     file://         — cp into the directory (creates if missing)
#     https://github.com/.../releases/download/TAG/  — gh release upload
#     anything else   — error (read-only backend)
cache_upload() {
  local base key asset manifest
  base="$(_strip_trailing_slash "$1")"
  key="$2"
  asset="$3"
  manifest="$4"
  case "$base" in
    file://*)
      local dir="${base#file://}"
      mkdir -p "$dir"
      cp "$asset"    "$dir/${key}.tar.zst"
      cp "$manifest" "$dir/${key}.manifest.json"
      ;;
    https://github.com/*/releases/download/*)
      # base = https://github.com/OWNER/REPO/releases/download/TAG
      local rest owner_repo tag
      rest="${base#https://github.com/}"
      owner_repo="${rest%%/releases/*}"
      tag="${rest#*/releases/download/}"
      gh release upload "$tag" "$asset" "$manifest" \
        -R "$owner_repo" --clobber
      ;;
    *)
      echo "cache_upload: only file:// or github.com Releases backends support writes; got: $base" >&2
      return 2
      ;;
  esac
}
