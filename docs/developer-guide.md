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
(`recipe.yaml` for metadata, `build.sh` for the build), and the
cache is a content-addressed store of tarballs keyed by a hash of
that directory plus `(version, os, arch)`. Same inputs → same key
→ same artifact, regardless of which CI run produced it.

Nothing about the cache is magical. A cached recipe is a
`<key>.tar.zst` plus a `<key>.manifest.json`, attached to a GitHub
Release on this repository. `setup-recipe` is a thin Action that
knows how to compute the key and HEAD-probe the asset.
`publish-recipe` is its inverse — runs the recipe's `build.sh`,
tar/zstd's the result, uploads it.

## When the cache moves, and when it doesn't

The recipe directory's content is the only knob. Edit
`recipes/llvm-asan/build.sh` — the key changes for every cell that
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

Two triggers feed `publish-recipe.yml`:

- **Push to `main`** that touches `recipes/`,
  `actions/setup-recipe/`, `actions/publish-recipe/`, or the
  workflow file. Iterates the cell matrix automatically;
  `skip-if-exists` keeps it idempotent so a no-op push costs one
  HEAD probe per cell.
- **Manual `workflow_dispatch`** for one-off cell warming —
  retrying a flaky build, populating a cell that just got added.

You almost never invoke `publish-recipe` directly. Most of the
time, when you change a recipe, the next push to `main` does the
right thing.

### Working locally

This is the part worth knowing about even if you never push to
ci-workflows. The `bin/recipe-cache` CLI is a self-contained
shell script that wraps the same code paths as the actions.
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
you edit `build.sh`, the key changes for every cell that uses
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
dispatching `publish-recipe` once for that cell or letting the
push trigger pick it up.

## Adding a new recipe

A recipe is a directory under `recipes/`. Two files:

- `recipe.yaml` — metadata read by `build-manifest.sh`. Keep it
  minimal. Today only `recipe`, `description`, and `source.{repo,
  branch_template}` are read; the verify workflow's
  `recipe-yaml-no-dead-fields` check enforces this.
- `build.sh` — the imperative build. Receives `RECIPE_VERSION`,
  `WORK_DIR`, `OUT_DIR` env vars; writes its result to
  `$OUT_DIR/llvm-project/` (or whatever subdirectory tree your
  recipe wants — `setup-recipe` and the CLI both surface the
  tarball root verbatim).

Verify locally with `bin/recipe-cache build` before pushing.
The verify workflow will catch the rest at PR time:

- `actionlint` over your edits to action / workflow files.
- `compute-key-parity` — your new key is stable across invocation
  contexts.
- `manifest-schema` — emitting valid JSON.
- `tar-zstd-round-trip` — the publish/consume pipelines round-trip
  bytewise.
- `end-to-end-fixture` — the CLI builds + caches + extracts a
  synthetic recipe.

When the recipe lands, add a row to `publish-recipe.yml`'s
push-trigger matrix so `main` warms it on every relevant push.

## Adding a new cell to an existing recipe

For now, edit the matrix in `publish-recipe.yml`. Add a row
matching the new (version, os, arch) tuple. The push trigger
takes care of the build on the next merge that touches the
recipe directory or the workflow file.

## Bumping the LLVM version

Change the `version` input on the `setup-recipe` call in your
client repo's matrix. The key changes; on the first PR run after
the bump, the recipe builds inline (`build-on-miss: true` does
the right thing); on the next push to ci-workflows main the new
cell gets warmed. The previous version's cell stays cached until
either it ages past `caps.grace_days` or you remove it from
`cells.yaml`, at which point `prune-cache` drops it. To evict
orphans before they age out — e.g. when storage is over `hard_gb`
and the grace window is holding too much back — dispatch
`prune-cache` manually with the `force` input enabled; this
bypasses `grace_days` for that one run and may break in-flight PRs
that referenced the dropped keys (they fall back to building from
source).

## Waking a self-hosted runner before a job

If your matrix targets a `[self-hosted, ...]` runner that isn't
always on (a Dell box on someone's desk, a workstation that
sleeps), `actions/wake-on-lan` sends the magic packet from a
spotter runner and waits for SSH (TCP port 22) on the target:

```yaml
jobs:
  wake-runner:
    # Spotter runner shares a LAN with the dell so the magic packet
    # reaches it via subnet broadcast.
    runs-on: [self-hosted, spotter]
    steps:
      - uses: compiler-research/ci-workflows/actions/wake-on-lan@<sha>
        with:
          mac: <hardware address>
          target-host: <ip address>
          # target-port: 22             # default; SSH = "ready"
          # broadcast: 192.168.100.255  # default derived from IPv4 target
          # port: 9                     # UDP WoL port; some old routers use 7
          # timeout-seconds: 240        # 4 minutes, checking every 10 s

  build:
    needs: wake-runner
    runs-on: [self-hosted, dell]
    ...
```

The action makes no assumptions about act -- it just sends the
packet. Consumers whose self-hosted runner is unreachable from act
(the typical case) don't need any guarding; act-only repro paths
target hosted-runner jobs that don't need the wake at all.

What the action does:
- Masks MAC/IP/broadcast in the run log (`::add-mask::`).
- Pre-checks the target via `bash /dev/tcp/$host/$port` -- skips
  the magic packet if the host is already responsive on the
  readiness port.
- Sends the magic packet via pure-stdlib Python UDP broadcast
  (no `apt-get install wakeonlan`, no `sudo` -- UDP sendto
  doesn't require privileges).
- Waits for the readiness port to start accepting TCP connects.

`bash /dev/tcp` is the portable readiness probe across GHA images
that lack `nc` or `ping`. Default port 22 corresponds to SSH being
up, which is the strongest signal that the runner is ready to
register itself with GitHub.

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

## Iterating on actions/ or recipes/ without pushing

End-to-end:

1. CI fails on a downstream PR. You'd rather not push another
   branch every iteration.
2. From the failing-PR repo: `bin/repro --list`. Failed rows on
   the current branch are tagged red; pick the row you care
   about.
3. `bin/repro <row-name>` runs that exact row inside docker via
   nektos/act. The shortcut handles workflow / job / matrix /
   container-arch / pre-flight collision detection.
4. The post-run shell drops you inside the container. Edit code,
   recompile, rerun the tests. On shell exit you're prompted to
   dump `git diff HEAD` to `/tmp/repro-<row>.patch` on the host;
   `git apply <patch>` brings the edits back to your working
   tree.
5. To test changes to *ci-workflows itself* (this repo) without
   pushing a branch, pass `--ci-workflows <local-path>` --
   bin/repro overlays your local `actions/` on the workflow.
6. Iterate. Push when green.

### What `--ci-workflows <path>` does

1. Copies every `actions/<name>/` from the local checkout to
   `<downstream>/.github/act-ci-workflows-stage/<name>/` (a copy
   rather than a symlink, because act doesn't follow directory
   symlinks for local actions).
2. Writes a temp workflow beside the original with each
   `uses: compiler-research/ci-workflows/actions/<name>@<ref>`
   rewritten to `uses: ./.github/act-ci-workflows-stage/<name>`.
3. Runs act on the temp workflow; removes the stage and temp file
   at exit.

`~/.cache/act/` is untouched, so you can keep multiple
ci-workflows checkouts on different branches and switch which one
bin/repro consumes via `--ci-workflows <path>`.

### Limits

- The downstream's `runs-on:` slugs need to dispatch under act
  (Linux containers; macOS / Windows rows skip).
- `<row-name>` resolves via fnmatch against what `act -n --json`
  enumerates; ambiguous matches print the candidates instead of
  running.
- act bind-mounts the consumer working tree, so workflow side
  effects (`build/`, `llvm-project/`, `__ci_workflows__/`) persist
  on the host after the container is removed. The workspace-clash
  pre-flight catches these on the next run; clean them up by hand
  for a pristine tree. Stage and temp workflow ARE cleaned at
  exit; if a run is killed hard, remove
  `.github/act-ci-workflows-stage/` and
  `.github/workflows/act-*-localized-*.yml` by hand.

## Iterating on LLVM with `--devshell`

`bin/repro <cell> --devshell` is a different mode: it doesn't run
a workflow. It downloads the cell's published install +
sibling-ccache + manifest, shallow-clones llvm-project at the
manifest's pinned `SRC_COMMIT`, and drops you into a long-lived
container ready for incremental rebuilds against the producer's
ccache.

Use it when:

- A workflow ran clean in CI but you want to edit something *in
  LLVM itself* and rebuild fast (the `bin/repro <row>` shell only
  reproduces the row's own build, which doesn't iterate well).
- You're triaging a cppyy / CppInterOp issue that needs a
  patched LLVM.
- You want to verify a recipe's published install actually compiles
  the next dependent layer (CppInterOp, cling) before relying on it.

### Cell argument

Either form works:

- A matrix-row name from a consumer repo (`bin/repro --list` from
  that repo enumerates them). Looked up against `act -n --json`,
  which gives `bin/repro` the recipe coord to download.
- A direct `recipe/version/os/arch` coord, e.g.
  `llvm-release/22/ubuntu-24.04/x86_64`. Use this when no consumer
  matrix references the cell yet (e.g. you just published it and
  haven't migrated downstream `setup-llvm` callers).

The cell is validated against `cells.yaml`; a typo fails fast
rather than 404'ing on Releases.

### Storage model — hermetic by default

The host sees only two paths from the running container:

1. **`$PWD` bound at `/patches` (rw).** Always on. AI inside writes
   `git format-patch -o /patches …`; you `git am` from `$PWD` on
   the host with your own identity. Refuses to launch if `$PWD ==
   $HOME` or resolves to `/`.
2. **`<host-cache>` bound at `/cache` (rw).** Opt-in via
   `--devshell-host-cache`. Carries persistent per-cell state
   AND the user's AI tooling. Layout:

   ```
   <host-cache>/                            default: ~/.cache/ci-workflows/devshell-cache/
     cells/<cell-id>/                       per-cell working data
       _recipe_out/install/                 install tree (LLVM_PREFIX)
       .ccache/                             producer's sibling ccache
       _recipe_work/llvm-project/           shallow llvm-project @ SRC_COMMIT
       manifest.json                        producer manifest
     ai/
       skills/                              user-curated skills (consumed inside via ~/.claude/skills symlink)
       settings.json                        user-curated settings (~/.claude/settings.json symlink)
       memory/<repo>/<encoded-host-path>/   per-project AI memory (~/.claude/projects/-patches/memory symlink)
   ```

Everything else — sources, build dir, ccache when host-cache is off,
shell history, container HOME — lives in a per-cell named docker
volume `devshell-<cell-id>` or inside the container's writable
layer. The volume survives `bin/repro --devshell --devshell-rm`;
reclaim with `docker volume rm devshell-<cell-id>`.

Inside the container the workspace is bind-mounted at the recipe's
runner workspace path (read from `manifest.build_env.ccache.base_dir`),
so ccache's recorded paths match the producer.

### Trust model

- No git identity is injected. The container has no `user.name`,
  `user.email`, ssh keys, or gpg keys. `git clone/fetch` works over
  public HTTPS; `git commit/push` will not (the AI must hand patches
  to the host).
- Default user is `dev` with host UID/GID. Files written to
  `/patches` come out owned by the host user, so `git am` works
  cleanly. `--devshell-as-root` is an escape hatch.

### Recommended setup (copy-paste)

The fastest path to a persistent, AI-enabled devshell. One-time
host setup, then a per-session loop. Replace `<cell>` with your
matrix-row name or `recipe/version/os/arch` coord, and
`/path/to/project` with whichever working copy you're patching.

```bash
# --- one-time host setup ------------------------------------------------
# Seed the host cache with your existing AI tooling. The container
# symlinks ~/.claude/{skills,settings.json,projects/-patches/memory}
# into this tree, so anything you put here is what the AI sees.
HOST_CACHE=~/.cache/ci-workflows/devshell-cache
mkdir -p "$HOST_CACHE/ai/skills" "$HOST_CACHE/ai/memory"
cp -r ~/.claude/skills/.       "$HOST_CACHE/ai/skills/"   2>/dev/null || true
cp    ~/.claude/settings.json  "$HOST_CACHE/ai/settings.json" 2>/dev/null || true

# --- per-session loop ---------------------------------------------------
cd /path/to/project                          # $PWD becomes /patches inside
bin/repro --devshell --devshell-host-cache <cell>
#   ... inside the container, run your AI of choice, iterate, then:
#       cd $DEVSHELL_SRC && git format-patch -o /patches <range>
#   ... exit when done.
git am /path/to/project/*.patch              # apply with your host identity
git push                                     # ...and ship as usual.

# --- teardown (optional) ------------------------------------------------
bin/repro --devshell --devshell-rm <cell>    # container only; volume kept
# docker volume rm devshell-<cell-id>        # reclaim the volume too
```

What this gets you:

- `cells/<cell-id>/` in the host cache persists src/build/ccache
  across sessions and across `--devshell-rm` cycles. Subsequent
  `bin/repro --devshell` re-enters in seconds, not minutes.
- `ai/memory/<repo>/<encoded-path>/` accumulates your AI's per-project
  knowledge on the host. It survives image rebuilds, machine moves,
  and `docker volume rm`. Treat it as part of your dotfiles.
- `ai/skills/` and `ai/settings.json` are the AI's personality. Curate
  them on the host; the container picks them up via symlink and stays
  hermetic.
- `/patches` is the only rw bind besides `/cache`. The AI literally
  cannot touch anything else on the host.

### Knobs

| flag | effect |
|------|--------|
| `--devshell-rm` | remove the container; named volume + host cache are kept |
| `--devshell-refetch` | re-download install/ccache/manifest into the volume / cache |
| `--devshell-host-cache` | bind `~/.cache/ci-workflows/devshell-cache/` at `/cache`. Required for persistent AI state across sessions. |
| `--devshell-host-cache-dir DIR` | as above, but bind `DIR` instead of the default location. |
| `--devshell-patches-out DIR` | override the `/patches` bind. Defaults to `$PWD`. |
| `--devshell-image IMAGE` | override the container image (prefer a digest pin). |
| `--devshell-as-root` | run the interactive shell as root. Files in `/patches` will be root-owned on the host. |
| `--devshell-script PATH` | run host PATH inside the container, exit with its rc (batch mode) |

### What `scripts/repro-config` does on entry

Idempotent — runs once per fetch, no-ops on rebuild:

1. **apt deps**: same set as `install-build-deps` Linux step
   (clang, cmake, ninja, ccache, libedit-dev, ...).
2. **libstdc++ auto-detect**: reads
   `manifest.cmake_state.CMakeCXXCompiler.cmake`, extracts the
   `CMAKE_CXX_IMPLICIT_INCLUDE_DIRECTORIES` path, and apt-installs
   the matching `libstdc++-N-dev` if it isn't local. Catches the
   catthehacker `libstdc++-13` vs GHA `libstdc++-14` drift that
   makes every C++ TU's preprocessed output diverge — 100%
   ccache-miss against the producer cache. For pre-`cmake_state`
   manifests, defaults to `libstdc++-14-dev` (matches the GHA
   `ubuntu-24.04` runner the recipes target).
3. **package-drift warning**: diffs the producer's
   `manifest.build_env.installed_packages` against local
   `dpkg-query` output, filtered to dev / clang / cmake / ninja /
   ccache / lld packages. Surfaces a `::warning::` line per
   divergent package; no auto-install.
4. **ccache `compiler_check`**: applies the producer's value
   verbatim (exported by `bin/repro` from
   `manifest.build_env.ccache.compiler_check`). Warns when the
   consumer's `$CC --version` diverges.
5. **cmake configure**: replays the recipe's own cmake invocation
   from `manifest.cmake_args`, substituting
   `CMAKE_INSTALL_PREFIX` and the source path. Pre-`cmake_args`
   manifests fall back to `llvm_build.base_cmake_args() +
   LLVM_ENABLE_PROJECTS=clang`.
6. **smoke compile**: builds
   `lib/Support/CMakeFiles/LLVMSupport.dir/Allocator.cpp.o`. Zero
   ccache hits ⇒ producer cache isn't reaching the consumer (drift
   the earlier checks didn't catch); surfaces a `::warning::`
   rather than aborting.

### Limits

- Linux Ubuntu cells only (`ubuntu-22.04`, `ubuntu-24.04`); other
  cell OSes refuse with a clear error.
- macOS hosts work via the Linux container, paying Rosetta
  emulation overhead on Apple Silicon.
- Pre-portable-ccache manifests (no `build_env.ccache`) provision
  correctly but miss on the first compile until a republish writes
  the portable-hashing config alongside.
