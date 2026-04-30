# Working with the recipe cache

A walk-through for the people who'll touch this code: contributors to
CppInterOp, clad, and cppyy on one side, and people who maintain the
recipe definitions on the other. Read this before reaching for the
README — the README is a quick reference, this is the why.

## What problem this is solving

Every CI run on CppInterOp / clad / cppyy spends most of its wall
clock building LLVM. That cost is mostly redundant: the LLVM tree is
the same across most matrix rows, and even when it isn't, the same
config is rebuilt across every PR for every project, every push, on
every runner. apt-llvm.org and Homebrew solve this for vanilla LLVM,
but the variants we actually need — sanitizer-instrumented LLVM,
LLVM cross-compiled to run inside wasm, the cling fork, eventually
MSan stacks and sanitizer-CPython — aren't redistributed by anyone
upstream. So we end up rebuilding them.

This repository caches those variants. The contract is small: a
recipe is a directory under `recipes/` with two files
(`recipe.yaml` for metadata, `build.py` for the build), and the
cache is a content-addressed store of tarballs keyed by a hash of
that directory plus `(version, os, arch)`. Same inputs → same key
→ same artifact, regardless of which CI run produced it.

Nothing about the cache is magical. A cached recipe is a
`<key>.tar.zst` plus a `<key>.manifest.json`, attached to a GitHub
Release on this repository. `setup-recipe` is a thin Action that
knows how to compute the key and HEAD-probe the asset.
`publish-recipe` is its inverse — runs the recipe's `build.py`,
tar/zstd's the result, uploads it.

## When the cache moves, and when it doesn't

The recipe directory's content is the only knob. Edit
`recipes/llvm-asan/build.py` — the key changes for every cell that
recipe produces, the next push to `main` rebuilds them, the cache
repopulates. Bump the LLVM version in your client repo's matrix
from `'22'` to `'23'` — the key changes (different `version`
input), `publish-recipe` builds the new cell, leaves the old one
alone until `prune-cache` garbage-collects it after `caps.grace_days`.

Things that *don't* move the key, deliberately:

- **The runner image SHA.** GitHub bumps these often; invalidating
  every cell on every bump would mean rebuilding LLVM on every
  Tuesday. The runner image is recorded in the manifest for
  forensics, so when something does break post-bump you can
  correlate.
- **External action versions** (`ccache-action`, `checkout`).
  Pinned to floating tags during iteration; sha-pin before the
  v1 contract freezes.
- **Wall clock.** Reproducibility outweighs freshness here.

The honest summary: the key tracks inputs we control inside the
recipe directory. Everything else gets logged but doesn't
invalidate.

## Three paths

You'll touch the cache in one of three ways depending on what
you're doing.

### Consuming from a CI workflow

Add a `setup-recipe` step to your matrix row:

```yaml
- uses: compiler-research/ci-workflows/actions/setup-recipe@<sha>
  with:
    recipe: llvm-asan
    version: '22'
    os: ${{ matrix.os }}
    arch: x86_64
```

On a hit you get the LLVM tree at `$GITHUB_WORKSPACE/llvm-project`
in seconds — a `curl | tar | zstd` pipe, nothing else. On a miss
the action falls through to building inline so your job doesn't
break before the cache is warmed; expect `~30 min` for a full
asan-LLVM build, less for cached partial work via ccache.

The `cache-base` input controls where to look. By default it's
this repository's Releases. Set it to `file:///abs/path/` to point
at a local directory (under `act`) or to
`http://lab.example.org/recipes/` to point at a team-internal
HTTP cache. The same key works against all of them.

### Producing from this repository

Two workflow files publish to the Releases cache, one per trigger:

- **`publish-recipe.yml`** — push to `main` that touches
  `cells.yaml`, `recipes/`, `actions/setup-recipe/`,
  `actions/publish-recipe/`, or the workflow file itself. Reads
  `cells.yaml`, probes the Releases cache for each cell, and only
  spawns per-cell runners for the cells whose key is missing. A
  no-op push costs one preflight runner (~30 s) and zero per-cell
  runners.
- **`publish-recipe-dispatch.yml`** — manual `workflow_dispatch`
  for one-off cell warming: retrying a flaky build, populating a
  cell that just got added. Single cell from inputs (recipe /
  version / os / arch).

You almost never invoke either workflow directly. Most of the
time, when you change a recipe, the next push to `main` does the
right thing.

### Working locally

This is the part worth knowing about even if you never push to
ci-workflows. The `bin/recipe-cache` CLI is a self-contained
Python script that imports the same modules as the actions
(`actions/lib/cache_io.py`, `actions/setup-recipe/compute_key.py`).
Defaults the backend to `file://` in `~/.cache/recipe-cache`, and
exposes the same operations:

```bash
# Run the recipe end-to-end. Real ~30-min asan build.
bin/recipe-cache build llvm-asan 22 ubuntu-24.04 x86_64

# Treat an existing build as if a recipe had produced it. Useful
# when you want to test the cache layer without paying for a
# fresh build.
bin/recipe-cache pack llvm-asan 22 darwin arm64 \
  --from /Users/me/work/builds/llvm-22-release

# Fetch + extract.
bin/recipe-cache get llvm-asan 22 ubuntu-24.04 x86_64 --out /tmp/llvm

bin/recipe-cache list             # show what's cached
bin/recipe-cache key  llvm-asan 22 ubuntu-24.04 x86_64
bin/recipe-cache rm   <full-key>
```

The cache directory is `~/.cache/recipe-cache` (override with
`RECIPE_CACHE_DIR`). It's just `<key>.tar.zst` and
`<key>.manifest.json` files — no database, no daemon, no lock
file.

> **Mockups aren't safe to share.** A `recipe-cache pack` tarball
> bears the same key shape as a real publish — `setup-recipe` will
> happily download and trust it. The manifest's `kind: mockup`
> field is documentation only, not enforced. Treat
> `~/.cache/recipe-cache` as machine-local; don't rsync mockup
> entries to a shared cache. (The publish path on the action side
> won't accept a mockup, but a hand-crafted upload could.)

To point a CppInterOp / clad / cppyy job at your local cache
when you run it under `act`:

```yaml
env:
  RECIPE_CACHE_BASE: file:///root/.cache/recipe-cache/
```

The same content addressing means "if your local cache holds
this key, the workflow will see a hit" — without ever touching
GitHub. Useful for testing recipe changes before pushing, for
working offline, for reproducing a CI failure on bare metal.

## Trade-offs you're accepting

The cache works because it isn't trying to be too clever. There
are four limits worth knowing about up front.

**Build trees aren't relocatable.** LLVMConfig.cmake stores
absolute paths to its imported targets. When you rsync your local
cache to a colleague whose home directory differs, `cmake` will
configure cleanly but `ninja` will fail at link time. For the
GHA case this is invisible — every runner extracts to
`$GITHUB_WORKSPACE/llvm-project`. For local use on the same
machine, also invisible. Cross-machine cache sharing is the case
that doesn't work today; we'll revisit if it actually matters.

**The first miss after a flag bump pays the build cost.** When
you edit `build.py`, the key changes for every cell that uses
that recipe. The push-to-main triggers `publish-recipe` to refill
them all in parallel — typically `~30 min` end-to-end, ccache
makes most of it cheap. Until that finishes, downstream PRs that
hit the new key fall through to inline build (`build-on-miss:
true` is the default). You may want to wait for the ci-workflows
merge to settle before merging downstream PRs touching the same
recipe.

**There is no auth on `https://` reads.** A team-internal HTTP
cache without TLS or with basic auth needs a wrapper. The lib's
`curl` invocation is bare; we'll add `RECIPE_CACHE_AUTH_HEADER`
env-var support when someone deploys a private host. Not a
priority until then.

**Recipe builds aren't host-portable for free.** The first cell
of a new recipe needs verification on each platform you intend to
publish for — cmake flag differences, ninja target name
differences, available libraries. `cells.yaml` enumerates which
combinations are first-class; every cell expansion is a manual
integration step done by adding a row to `cells.yaml` and either
dispatching `publish-recipe-dispatch.yml` once for that cell or
letting the push trigger pick it up.

## Adding a new recipe

A recipe is a directory under `recipes/`. Two files:

- `recipe.yaml` — metadata read by `build_manifest.py`. Keep it
  minimal. Today only `recipe`, `description`, and `source.{repo,
  branch_template}` are read; the verify workflow's
  `recipe-yaml-no-dead-fields` check enforces this.
- `build.py` — the imperative build. Receives `RECIPE_VERSION`,
  `WORK_DIR`, `OUT_DIR` env vars; writes its result to
  `$OUT_DIR/llvm-project/` (or whatever subdirectory tree your
  recipe wants — `setup-recipe` and the CLI both surface the
  tarball root verbatim). Build scripts typically import
  `actions/lib/llvm_build.py` for shared scaffolding (env
  validation, cmake flags, install-distribution, smoke).

Verify locally with `bin/recipe-cache build` before pushing.
The verify workflow will catch the rest at PR time:

- `actionlint` over your edits to action / workflow files.
- `python-unit-tests` — cross-OS unit tests for every Python
  module in `actions/lib/`, `actions/setup-recipe/`,
  `actions/publish-recipe/`, and `bin/`.
- `recipe-yaml-no-dead-fields` — every top-level key in
  `recipe.yaml` is read by something.
- `cells-yaml-integrity` — schema + duplicate-cell + recipe-
  existence checks on `cells.yaml`.
- `recipe-smoke` — `RECIPE_QUICK_CHECK=1 build.py` for every
  cells.yaml entry on its native OS (cmake configure +
  LLVMDemangle compile).
- `publish-dryrun` — end-to-end dry run of the
  `actions/publish-recipe` action against the `llvm-dry-run`
  fixture with `cache-base: file://`, exercising every shared
  codepath a real publish runs.

When the recipe lands, add a row to `cells.yaml` so `main` warms
it on every relevant push.

## Adding a new cell to an existing recipe

Edit `cells.yaml`. Add a row matching the new (version, os, arch)
tuple. `publish-recipe.yml`'s preflight job reads `cells.yaml`
directly, so the next push to `main` that touches the recipe
directory or the workflow file picks up the new cell automatically.

## Bumping the LLVM version

Change the `version` input on the `setup-recipe` call in your
client repo's matrix. The key changes; on the first PR run after
the bump, the recipe builds inline (`build-on-miss: true` does
the right thing); on the next push to ci-workflows main the new
cell gets warmed. The previous version's cell stays cached until
either it ages past `caps.grace_days` or you remove it from
`cells.yaml`, at which point `prune-cache` drops it.

## Inspecting a published asset

The manifest sibling tells you what produced any given tarball:

```bash
gh release view cache -R compiler-research/ci-workflows \
  | grep manifest.json
gh release download cache -R compiler-research/ci-workflows \
  -p '<key>.manifest.json'
jq . <key>.manifest.json
```

Manifests record: the source repository and commit, the recipe
file content hashes, the runner image and version, the
ci-workflows commit that built it, the build timestamp. If a
cached binary surprises you in the field, the manifest is where
you start.
