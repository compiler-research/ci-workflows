# ci-workflows

Common CI infrastructure for compiler-research projects (CppInterOp,
clad, cppyy). Provides a content-addressed cache of prebuilt LLVM-
family recipe artifacts that the upstream ecosystem doesn't
redistribute (sanitizer-instrumented LLVM, wasm-LLVM, the cling fork,
eventually MSan stacks and sanitizer-CPython).

> **New here?** Read [docs/developer-guide.md](docs/developer-guide.md)
> first. It walks through why this exists, how to use it day-to-day,
> and the trade-offs you're accepting. The rest of this README is a
> quick reference.

## Consumer side: `setup-recipe`

In a downstream workflow:

```yaml
- uses: compiler-research/ci-workflows/actions/setup-recipe@<sha-or-tag>
  with:
    recipe: llvm-asan
    version: '22'
    os: ubuntu-24.04
    arch: x86_64
```

On a hit, the prebuilt LLVM tree lands at `$GITHUB_WORKSPACE/llvm-project`.
On a miss, the action falls through to building inline from source so
CI doesn't break before the cache is warmed.

## Producer side: `publish-recipe`

The `publish-recipe.yml` workflow runs on:

- **Push to `main`** that touches `recipes/`, `actions/setup-recipe/`,
  `actions/publish-recipe/`, or the workflow file itself. Iterates a
  matrix of cells and uploads any whose key isn't yet in the Releases
  cache. `skip-if-exists` keeps the steady-state cost to one HEAD
  probe per cell.
- **Manual `workflow_dispatch`** for one-off cell warming.

## Local testing

The same cache contract works against a local directory. The
`bin/recipe-cache` CLI is a self-contained shell wrapper around the
same scripts the actions use.

```bash
# Build a recipe locally — full ~30 min for asan.
bin/recipe-cache build llvm-asan 22 ubuntu-24.04 x86_64

# Or, point at an existing build to mock up a cache entry without
# rebuilding (useful for testing the cache layer end-to-end).
bin/recipe-cache pack llvm-asan 22 darwin arm64 \
  --from /path/to/existing/llvm-project-build

# Fetch + extract.
bin/recipe-cache get llvm-asan 22 ubuntu-24.04 x86_64 --out /tmp/llvm
# Recipes publish a cmake --install tree, so LLVMConfig.cmake lives at
# the standard install path — pass this directory to find_package(LLVM).
ls /tmp/llvm/llvm-project/lib/cmake/llvm/

# Inspect cached cells.
bin/recipe-cache list
```

The cache lives in `$RECIPE_CACHE_DIR` (default `~/.cache/recipe-cache`)
as plain `<key>.tar.zst` + `<key>.manifest.json` files. Anyone can
share their cache directory: rsync to a colleague, host it on an
internal webserver, mount it via NFS — the directory shape is the
same regardless.

### Pointing client workflows at a local cache

Either set `RECIPE_CACHE_BASE` in the workflow's environment, or pass
the `cache-base` input directly:

```yaml
# In a CppInterOp workflow run via act (or any local runner):
env:
  RECIPE_CACHE_BASE: file:///root/.cache/recipe-cache/
```

Or:

```yaml
- uses: compiler-research/ci-workflows/actions/setup-recipe@<sha>
  with:
    recipe: llvm-asan
    version: '22'
    os: ubuntu-24.04
    arch: x86_64
    cache-base: file:///root/.cache/recipe-cache/
```

A team-internal HTTP cache works the same way — point `cache-base` at
`https://lab.example.org/recipes/`. Reads use `curl`; writes via this
URL are read-only at the moment (only `file://` and `gh release upload`
are supported sinks).

## Reproducing a CI failure locally

When a matrix row fails on a downstream PR, `bin/repro` runs that
exact row inside docker via [nektos/act](https://github.com/nektos/act)
— no branch push, no waiting for CI:

```bash
cd ~/sources/CppInterOp                     # the failing-PR repo
bin/repro --list                            # jobs + cell-cache hits
                                            # + red [failed] tags
bin/repro <row-name>                        # reproduce one row
```

The row-name shortcut resolves to `-W <workflow> -j <job> -m
name:<row>` via fnmatch against `act -n --json`. `bin/repro` picks
the right `--container-architecture` from the row's `os:` slug,
refuses to launch when stale `build/` or `llvm-project/` in cwd
would collide with the workflow's `mkdir`, and drops you into a
shell inside the post-run container. On shell exit you're prompted
to dump any in-container edits as a patch on the host.

Iterate on a `ci-workflows` action or recipe without pushing:

```bash
~/sources/ci-workflows/bin/repro \
    --ci-workflows ~/sources/ci-workflows \
    <row-name>
```

`--ci-workflows` stages the local recipes and actions into the consumer
repo and rewrites every ci-workflows action `uses:` — both the
workflow's top-level ones and those nested inside a staged composite
action (e.g. `setup-kokkos` → `setup-recipe`) — to the staged copy;
`setup-recipe` sources recipes from the stage instead of git-fetching.
So an un-pushed action or recipe is exercised end-to-end. Stage /
temp-workflow files are cleaned at exit; disk after the run is zero
(image cache aside). See `bin/repro --help` and
[docs/developer-guide.md](docs/developer-guide.md) for the rest.

## Iterating on LLVM with a warm ccache: `--devshell`

`bin/repro <cell> --devshell` skips the workflow and instead drops
you into a long-lived container with the cell's published install,
sibling ccache, and matching LLVM source already in place. Edits to
`llvm-project/` rebuild incrementally against the producer's cache,
so a single TU changes in seconds rather than the ~30 minutes a
cold compile would take.

```bash
bin/repro ubu24-x86-gcc14-cling-llvm20-cppyy --devshell
# inside the container:
cd $DEVSHELL_BUILD && ninja clang
```

The cell argument is either a matrix-row name (validated against
`act -n --json` for the consumer repo) or a direct
`recipe/version/os/arch` coord (e.g. `llvm-release/22/ubuntu-24.04/x86_64`)
for cells no consumer matrix references yet. Files live under
`~/.cache/ci-workflows/devshell/<cell>/`; the container is named
`devshell-<cell>` and persists across invocations. Common knobs:

| flag | effect |
|------|--------|
| `--devshell-rm` | remove the container; preserve the host workdir |
| `--devshell-refetch` | re-download install / ccache / manifest |
| `--devshell-script PATH` | run PATH inside the container instead of an interactive shell (CI / smoke use) |

`scripts/repro-config` runs at first entry and on each subsequent
fetch: it installs the same apt deps as `install-build-deps`,
auto-installs the libstdc++-N-dev that matches the producer's
`/usr/include/c++/N` (catches the ~100% ccache-miss class caused by
catthehacker's libstdc++-13 vs GHA's libstdc++-14), applies the
producer's ccache `compiler_check`, replays the recipe's own cmake
invocation from `manifest.cmake_args`, and warns on dev-package
version drift. A smoke compile of `lib/Support/Allocator.cpp.o`
verifies that the producer cache actually reaches the consumer
environment before handing off the shell.

Linux-only for now (Ubuntu cells); macOS hosts work via the bundled
Linux container, with the platform-mismatch overhead under Rosetta.

## Layout

```
recipes/<name>/
  recipe.yaml          metadata fields the manifest reads
  build.py|build.sh    imperative build invoked by setup-recipe and publish-recipe
  patches/             optional, applied to the source tree

actions/
  setup-recipe/        consumer-side: probe → download or build-on-miss
  setup-kokkos/        consumer-side: setup-recipe wrapper that installs Kokkos and exports Kokkos_ROOT
  publish-recipe/      producer-side: build under ccache + tar/zstd + upload
  wake-on-lan/         send a magic packet to wake a self-hosted runner; no-op under act
  lib/cache-io.sh      scheme-aware probe/download/upload helpers; sourced by both actions and the CLI
  install-build-deps/  thin composite action installing host packages

bin/recipe-cache       CLI wrapping the same scripts the actions use

.github/workflows/
  publish-recipe.yml   workflow_dispatch + push-on-main publisher
```
