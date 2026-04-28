#!/usr/bin/env bash
# Emits the JSON manifest describing a published recipe build.
#
# Args: RECIPE VERSION OS ARCH KEY
# Reads (env): SRC_COMMIT (set by the recipe's build.sh)
#              GITHUB_SHA (the ci-workflows commit being built from)
#              ImageOS, ImageVersion (runner image identifiers)
set -euo pipefail

RECIPE="$1"; VERSION="$2"; OS="$3"; ARCH="$4"; KEY="$5"

recipe_yaml_sha=$(sha256sum "recipes/${RECIPE}/recipe.yaml" | awk '{print $1}')
build_sh_sha=$(sha256sum "recipes/${RECIPE}/build.sh"     | awk '{print $1}')

# Best-effort: surface the source repo + branch as recorded in recipe.yaml.
# We don't parse YAML in shell — keep it tagged in the manifest for humans;
# the source commit is the authoritative reproducibility anchor.
src_repo=$(grep -E '^\s*repo:'           "recipes/${RECIPE}/recipe.yaml" | head -1 | sed -E 's/.*repo:\s*//' | tr -d '"')
src_branch_tpl=$(grep -E '^\s*branch_template:' "recipes/${RECIPE}/recipe.yaml" | head -1 | sed -E 's/.*branch_template:\s*//' | tr -d '"')
src_branch="${src_branch_tpl//\{version\}/$VERSION}"

cat <<EOF
{
  "key": "${KEY}",
  "recipe": "${RECIPE}",
  "version": "${VERSION}",
  "platform": {
    "os": "${OS}",
    "arch": "${ARCH}",
    "runner_image": "${ImageOS:-unknown}",
    "runner_image_version": "${ImageVersion:-unknown}"
  },
  "recipe_yaml_sha256": "${recipe_yaml_sha}",
  "build_sh_sha256": "${build_sh_sha}",
  "source": {
    "repo": "${src_repo:-unknown}",
    "branch": "${src_branch:-unknown}",
    "commit": "${SRC_COMMIT:-unknown}"
  },
  "ci_workflows_sha": "${GITHUB_SHA:-unknown}",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
