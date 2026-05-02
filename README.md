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

Iterate on a `ci-workflows` action without pushing:

```bash
~/sources/ci-workflows/bin/repro \
    --ci-workflows ~/sources/ci-workflows \
    <row-name>
```

Stage / temp-workflow files are cleaned at exit; disk after the
run is zero (image cache aside). See `bin/repro --help` and
[docs/developer-guide.md](docs/developer-guide.md) for the rest.

## Layout

```
recipes/<name>/
  recipe.yaml          metadata fields the manifest reads
  build.sh             imperative build invoked by setup-recipe and publish-recipe
  patches/             optional, applied to the source tree

actions/
  setup-recipe/        consumer-side: probe → download or build-on-miss
  publish-recipe/      producer-side: build under ccache + tar/zstd + upload
  wake-on-lan/         send a magic packet to wake a self-hosted runner; no-op under act
  lib/cache-io.sh      scheme-aware probe/download/upload helpers; sourced by both actions and the CLI
  install-build-deps/  thin composite action installing host packages

bin/recipe-cache       CLI wrapping the same scripts the actions use

.github/workflows/
  publish-recipe.yml   workflow_dispatch + push-on-main publisher
```
