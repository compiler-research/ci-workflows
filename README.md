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

## Just want to start working on a project? `bin/start`

You need Docker, git and a Python 3. Not `act`, and not a toolchain --
that gets downloaded.

```bash
git clone https://github.com/compiler-research/ci-workflows
cd ci-workflows && ./bin/start
```

It lists the projects in [`projects.yaml`](projects.yaml) with the
toolchain each develops against, clones the one you pick, and opens a
container holding that exact toolchain -- the same artifact the
project's CI uses -- with its host dependencies installed and Claude
Code ready. Run it from inside a checkout you already have and it skips
the menu.

Your checkout is bind-mounted, not copied, so edits show up on the host
immediately and you commit, push and open pull requests from there.
No credentials are copied into the container.

Not in the list? Any repository whose CI uses ci-workflows works --
your fork of one of them, for example, either way round:

```bash
./bin/start --repo yourname/clad          # owner/repo, clone URL or path
cd ~/src/my-fork && ~/src/ci-workflows/bin/start
```

It reads the repository's own workflows, lists the toolchains its CI
rows pull, and offers the plainest one first.

`./bin/start --list` prints the catalog without prompting
(`--list --repo <path>` does the same for a checkout). See
[Onboarding a contributor](docs/developer-guide.md#onboarding-a-contributor-binstart)
for what it sets up and how to add a project to the list.

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

## Iterating on a recipe with a warm ccache: `--devshell`

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

The shell opens in `/patches`, your own checkout, with a short note on
where the toolchain and the recipe's sources are, and on how to install
Claude Code if it is missing (`curl -fsSL https://claude.ai/install.sh |
bash`, which works without sudo).

The cell argument is either a matrix-row name of the consumer repo you
run it from (its cell is read out of that repo's own workflows, no act
involved) or a direct `recipe/version/os/arch` coord (e.g.
`llvm-release/22/ubuntu-24.04/x86_64`) for cells no consumer matrix
references yet. A row whose LLVM comes from apt/brew (setup-llvm
`flavor: system`) has no cell, and says so. The cell's install,
ccache and source live in a per-cell docker volume `devshell-<cell-id>`
(e.g. `devshell-llvm-release-22-ubuntu-24.04-x86_64`), or, with
`--devshell-host-cache`, on the host under
`~/.cache/ci-workflows/devshell-cache/cells/<cell-id>/`, where they
survive `docker volume rm` ([storage model](docs/developer-guide.md#storage-model--hermetic-by-default)).
The container has the same name and persists across invocations.
Common knobs:

| flag | effect |
|------|--------|
| `--devshell-rm` | remove the container; the volume and host cache are kept |
| `--devshell-refetch` | re-download install / ccache / manifest |
| `--devshell-script PATH` | run PATH inside the container instead of an interactive shell (CI / smoke use) |
| `--devshell-install PKG...` | apt-install packages into the devshell as root, then exit; a failed `apt-get install` inside prints this command for you |
| `--devshell-sudo` | give `dev` passwordless sudo (off by default: it makes the AI root in the container) |
| `--devshell-writable-git` | mount `/patches/.git` read-write so git inside can commit (off by default: commit on the host) |

`scripts/repro-config` runs at first entry and on each subsequent
fetch: it installs the same apt deps as `install-build-deps`,
auto-installs the libstdc++-N-dev that matches the producer's
`/usr/include/c++/N` (catches the ~100% ccache-miss class caused by
catthehacker's libstdc++-13 vs GHA's libstdc++-14), applies the
producer's ccache `compiler_check`, replays the recipe's own cmake
invocation from `manifest.cmake_args`, warns on dev-package version
drift, and reproduces the producer's locale (below). A smoke compile of
`lib/Support/Allocator.cpp.o` verifies that the producer cache actually
reaches the consumer environment before handing off the shell; it
counts only a hit on one of the producer's own entries, not on one an
earlier session wrote.

ccache hashes `LANG`, `LC_ALL`, `LC_CTYPE` and `LC_MESSAGES` into every
key, so a shell whose locale differs from the producer's misses the
whole cache -- the llvm-release 22 cell was built under
`LANG=C.UTF-8`, and a devshell without it got 0 hits. (LLVM >= 23
cells had a second problem: their builds use precompiled headers, which
ccache refuses to cache without `sloppiness=pch_defines,time_macros`, so
cells published before `publish-recipe` set it hold almost nothing and
need republishing. The devshell and `setup-recipe` set it too, so their
own rebuilds are cached regardless.) Manifests now
record the producer's values (`build_env.ccache.locale`); for older
ones `repro-config` finds them by replaying the smoke compile under
each candidate. The result goes to `/etc/devshell-env.sh`, which every
shell in the container sources (via `BASH_ENV` and `/etc/bash.bashrc`).

Works for any recipe, not only the LLVM ones: the source directory
comes from the recipe's `source.repo` and the manifest's recorded cmake
invocation is replayed with producer paths rewritten locally.

`--devshell` reads from whatever cache base is set, so it also works
against an artifact you built yourself rather than a published cell:

```bash
bin/recipe-cache pack <recipe> <version> <os> <arch> --from /path/to/install
RECIPE_CACHE_BASE=file://$HOME/.cache/recipe-cache/ \
  bin/repro <recipe>/<version>/<os>/<arch> --devshell
```

A packed entry has no sibling ccache and no cloneable source, so the
devshell starts cold and without a checkout -- see
[docs/developer-guide.md](docs/developer-guide.md).

Linux-only for now (Ubuntu cells); macOS hosts work via the bundled
Linux container, with the platform-mismatch overhead under Rosetta.

## Reusing that ccache from a workflow: `fetch-ccache`

The same sibling snapshot is what `setup-llvm`'s `fetch-ccache` input
restores, for a consumer that cannot use the install tree because it
has to compile LLVM's own sources again -- built in tree beside the
consumer (`LLVM_EXTERNAL_PROJECTS`), or with a generator the artifact
was not produced with. The rebuild then costs minutes rather than the
half-hour a cold one does.

```yaml
- uses: compiler-research/ci-workflows/actions/setup-llvm@main
  with:
    version: '21'
    os: ubuntu-24.04
    fetch-ccache: 'true'
```

Three things have to hold or the lookups miss, and a miss is silent --
the build simply takes half an hour:

* **ccache on PATH** before the action runs. It says so and restores
  nothing otherwise.
* **The source at the producer's relative path.** `publish-recipe`
  hashes with `hash_dir=false` and `base_dir` at its workspace root,
  so an entry is keyed on the path relative to that root. The recipes
  clone to `_recipe_work/<name>`, so a consumer wants
  `$GITHUB_WORKSPACE/_recipe_work/<name>` too. The action reads the
  producer's own ccache settings from the manifest and applies them;
  the layout is the consumer's to match.
* **The producer's compile flags.** ccache hashes the whole command
  line, so replay the manifest's `cmake_args` and add to them rather
  than writing a configure of your own -- the same rule
  `scripts/repro-config` follows for the devshell.
* **The producer's locale.** ccache hashes `LANG`/`LC_*` too. The
  action exports what the manifest records (`build_env.ccache.locale`)
  to the rest of the job, and warns when the producer had a variable
  unset that this runner sets, which it cannot undo.

Worth copying the devshell's check too: compile one TU every LLVM
build has (`lib/Support/CMakeFiles/LLVMSupport.dir/Allocator.cpp.o`)
and fail if `ccache --show-stats` reports no hits. Otherwise the row
still passes and only the clock says something is wrong.

No sibling is published for Windows cells, so `fetch-ccache` is a
no-op with a notice there.

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
  lib/                 python helpers: cache_io.py (scheme-aware probe/download/upload), llvm_build.py (shared LLVM build flow)

bin/recipe-cache       CLI wrapping the same scripts the actions use

.github/workflows/
  publish-recipe.yml   workflow_dispatch + push-on-main publisher
```
