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

## Adding a project to `bin/start`

Add an entry to `projects.yaml`: the clone URL, the matrix row a new
contributor should develop against, and the cell that row resolves to.
`verify.yml` fails the PR if the cell is not in `cells.yaml`, so a
typo or a decommissioned cell cannot reach a student.

Pick the plainest Linux row -- not a sanitizer, cross-compile or
self-hosted one. This is the environment somebody meets the project in,
not a matrix audit.

The cell is written out so the menu can show each project's toolchain
and download size before anything is cloned. To get it, run
`bin/start --list --repo <checkout>` on a clone of the project: it
lists the cell every row resolves to. The `workflow` and `row` fields
say where the cell came from. Once the project is cloned, `bin/start`
reads that row back out of the project's own workflow with
`bin/cells.py`, and the project wins if it disagrees, whichever of
recipe, version, os or arch has moved. So the catalog going a little
stale is self-healing rather than harmful.

clad's entry is a worked example: row `ubu24-clang20-runtime23`,
which clad's `ci.yml` resolves to `llvm-release/23/ubuntu-24.04/x86_64`.

A project whose CI does not use ci-workflows -- ROOT, LLVM itself --
has no row to point at: leave `workflow` and `row` out and the cell in
the entry is used as is. A large repository can say how to clone it
with `clone`, limited to a partial-clone filter and a branch; llvm-23
uses `--filter=blob:none --branch=release/23.x`. An entry with a
branch matches a checkout only while it is on that branch, so an
llvm-project clone on a feature branch gets the menu rather than
LLVM 23.

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

## Onboarding a contributor: `bin/start`

Everything below this line assumes you know which cell you want and
which flags make it persist. `bin/start` is the same machinery for
somebody who does not: it reads `projects.yaml`, shows each project
with its toolchain and whether that cell is published, and hands the
selection to `--devshell`. It is a front end, not a second
implementation. It builds the `bin/repro --devshell ...` command line
you could type yourself, runs it through `repro.main(argv)`, and prints
it so you can reopen the shell with repro directly.

Prerequisites are Docker, git and a Python 3. Not `act`: the cell goes
to repro as a direct coordinate, which needs no matrix lookup.

Two entry paths, one command:

```bash
# from nothing: clone this repo, pick from the menu, it clones for you
git clone https://github.com/compiler-research/ci-workflows
cd ci-workflows && ./bin/start

# from a checkout you already have: no menu, no prompts
cd ~/src/CARTopiaX && ~/src/ci-workflows/bin/start
```

The second form matches on the `origin` remote rather than the
directory name, so forks and renamed directories resolve.

### Repositories outside the catalog

A repository that is not in `projects.yaml` still works if its CI uses
ci-workflows -- a fork, say, since the catalog matches on the exact
`origin` owner/repo:

```bash
./bin/start --repo yourname/clad           # owner/repo, URL or path; clones if needed
cd ~/src/my-fork && ~/src/ci-workflows/bin/start  # uncatalogued checkout: no menu
./bin/start --list --repo ~/src/my-fork    # what it would offer, no prompts
```

The menu also takes `o` for "another repository".

Such a repository has no recorded cell, so one is read out of its
workflows: every `setup-llvm` / `setup-recipe` call is evaluated against
every matrix row, step `if:`s included, walking into composites such as
`setup-biodynamo` and `setup-cuda`. Evaluating the consumer's own
expressions is what makes this convention-free, and the conventions do
differ: clad writes `use-recipe: 'true'` and defaults its flavor to
`system`, while CppInterOp passes `matrix.flavor` straight through, so
an absent flavor means llvm-release there.

Each resulting cell is checked against `cells.yaml` and against what
`--devshell` can open (the ubuntu-24.04 and ubuntu-22.04 runner images
today). Usable cells are listed plainest first -- llvm-release before
its sanitizer and debug variants, then by how many rows use it -- and
the rest with the reason they can't be used. A repository whose rows
only use `flavor=system` is told there is nothing to download;
`bin/repro <row>` replays such a row under act instead.

### Where the cell-resolution code lives

One implementation, used by both `bin/repro` (`--devshell <row>`, the
`[cell: ...]` tags in `--list`) and `bin/start`:

| file | knows about |
| --- | --- |
| `bin/gha.py` | GitHub Actions only: YAML, `${{ }}` expressions, matrix expansion, step `if:`, walking into composite actions. Nothing about recipes. |
| `bin/cells.py` | Recipes: the coord type, `cells.yaml`, one resolver per action that picks its recipe in shell, row scanning, "plainest first" ranking. |

To support a new action, see the module docstring of `bin/cells.py`.
An action that only forwards to one we already resolve needs nothing,
because the walk goes into it. An action that picks its recipe in a
shell step needs a resolver registered with `@resolves("<action>")`:
a function from the call's evaluated inputs to a coord.

`setup-llvm` is that case today. Its "Resolve flavor → recipe" `case`
is restated as `cells.FLAVOR_TO_RECIPE`, and `bin/test_cells.py` parses
the action and fails if the two differ, so a new flavor added to the
action without the table breaks the test rather than `bin/start`.
Recipes a row layers on top of its toolchain (`cuda-headers`) are
listed in `cells.ADDON_RECIPES`. They never count as a row's cell on
their own, so a row that takes LLVM from apt and headers from the cache
is reported as having no cell.

`bin/start` drives `bin/repro` only through its command line,
`repro.main(argv)`, built by `devshell_argv()`. So a change inside
repro can't break start as long as the command line a user would type
still works, and `bin/test_start.py` checks that argv against repro's
real parser.

## Iterating on a recipe with `--devshell`

`bin/repro <cell> --devshell` is a different mode: it doesn't run
a workflow. It downloads the cell's install + sibling-ccache +
manifest, shallow-clones the recipe's source at the manifest's
pinned `SRC_COMMIT`, and drops you into a long-lived container
ready for incremental rebuilds against the producer's ccache.

The sibling ccache has a second consumer, in CI rather than at a
prompt: `setup-llvm`'s `fetch-ccache` restores it into a workflow, for
a row that has to compile the recipe's sources again in a
configuration the install tree cannot express. It mirrors the
producer's `hash_dir`/`base_dir` settings and its locale (`LANG`/`LC_*`,
which ccache hashes into every key) the way `repro-config` does,
and leaves the two things it cannot control -- the source's relative
path and the configure flags -- to the consumer. See the README
section for what a row has to match.

This works for **any** recipe, not only the LLVM ones. The source
directory is derived from the recipe's `source.repo` (so
`llvm-*` land in `_recipe_work/llvm-project`, `kokkos` in
`_recipe_work/kokkos`, `biodynamo` in `_recipe_work/biodynamo`),
and the cmake invocation recorded in the manifest is replayed with
its producer-side paths rewritten to local ones — see
`actions/lib/devshell_cmake.py`.

Use it when:

- A workflow ran clean in CI but you want to edit something *in the
  recipe's own source* and rebuild fast (the `bin/repro <row>` shell
  only reproduces the row's own build, which doesn't iterate well).
- You're triaging a cppyy / CppInterOp issue that needs a
  patched LLVM.
- You want to verify a recipe's published install actually compiles
  the next dependent layer (CppInterOp, cling, a simulation built
  against BioDynaMo) before relying on it.
- You want to debug an artifact that is *not published yet* — see
  "Devshell against a local build" below.

### Devshell against a local build

`--devshell` reads from whatever cache base is configured, so it
does not require a published cell. To debug an artifact you built
yourself:

```bash
# 1. Pack an existing install tree into the local cache.
bin/recipe-cache pack <recipe> <version> <os> <arch> --from /path/to/install

# 2. Point the devshell at it.
RECIPE_CACHE_BASE=file://$HOME/.cache/recipe-cache/ \
  bin/repro <recipe>/<version>/<os>/<arch> --devshell
```

Two things are deliberately tolerated on this path, because a
packed entry is not a published one:

- **No sibling ccache.** `publish-recipe` emits `<key>.ccache.tar.zst`;
  `recipe-cache pack` does not. The fetch logs that it is missing and
  continues, so the devshell starts cold rather than failing.
- **No source tree.** `pack` stamps a placeholder `source.repo` of
  `mockup://` that cannot be cloned, so the clone is skipped and you
  get a container with the install and no checkout. That is the point
  of the mode: inspecting or *running* a packed artifact.

### Cell argument

Either form works:

- A matrix-row name from the consumer repo you run it in (`bin/repro
  --list` there enumerates them, tagging each with its cell).
  `bin/cells.py` reads the row's cell out of the repo's own workflows
  (see [Where the cell-resolution code lives](#where-the-cell-resolution-code-lives)),
  so this needs no act. A row on `flavor: system` has no cell and
  fails with that reason. A glob instead of an exact name still goes
  through act's matrix listing first.
- A direct `recipe/version/os/arch` coord, e.g.
  `llvm-release/22/ubuntu-24.04/x86_64`. Use this when no consumer
  matrix references the cell yet (e.g. you just published it and
  haven't migrated downstream `setup-llvm` callers).

The cell is validated against `cells.yaml`; a typo fails fast
rather than 404'ing on Releases.

### Storage model — hermetic by default

The host sees only these paths from the running container:

1. **`$PWD` bound at `/patches` (rw), its `.git` read-only.** Always
   on. Edit in the container; commit, push and `git am` on the host
   with your own identity. `.git` is read-only because git on the host
   acts on its config and hooks (`--devshell-writable-git` to opt out).
   Patches of the recipe's *own* source go the same way:
   `git -C $DEVSHELL_SRC format-patch -o /patches …`. Refuses to launch
   if `$PWD == $HOME` or resolves to `/`.
2. **Parts of `<host-cache>`.** Opt-in via `--devshell-host-cache`:
   the cell's working data as the workspace, and the AI tooling under
   `/cache` -- skills and settings read-only, and only this project's
   memory directory read-write. Nothing else of the host cache is
   mounted: not other cells, not other projects' memory, and not
   `manifests/`, the copies the host itself acts on. Layout:

   ```
   <host-cache>/                            default: ~/.cache/ci-workflows/devshell-cache/
     cells/<cell-id>/                       this cell's working data -> the workspace (rw)
       _recipe_out/install/                 install tree (LLVM_PREFIX)
       .ccache/                             producer's sibling ccache
       _recipe_work/llvm-project/           shallow llvm-project @ SRC_COMMIT
       manifest.json                        the container's copy of the manifest
     manifests/<key>.json                   the host's copy; never mounted
     ai/
       skills/                              -> /cache/ai/skills (ro; ~/.claude/skills symlink)
       settings.json                        -> /cache/ai/settings.json (ro; ~/.claude/settings.json)
       memory/<repo>/<encoded-host-path>/   -> the same path under /cache (rw; ~/.claude/projects/-patches/memory)
   ```

   bin/repro refuses to bind a path with a symlink in it below the host
   cache, and runs git on the host only to create a checkout; an
   existing one is updated from inside the container.

Everything else — sources, build dir, ccache when host-cache is off,
shell history, container HOME — lives in a per-cell named docker
volume `devshell-<cell-id>` or inside the container's writable
layer. The volume survives `bin/repro --devshell --devshell-rm`;
reclaim with `docker volume rm devshell-<cell-id>`.

Inside the container the workspace is bind-mounted at the recipe's
runner workspace path (read from `manifest.build_env.ccache.base_dir`),
so ccache's recorded paths match the producer.

### Container restrictions

The devshell runs the AI with the user's source tree in reach, so the
container is restricted by default. All of it is built in one place,
`bin/sandbox.py` plus `_devshell_security` / `_devshell_mounts` in
`bin/repro`, and `scripts/devshell-posture-check` verifies it from the
inside (verify.yml's devshell-smoke job runs it):

- `dev` has no sudo, and the container runs under `no-new-privileges`.
  `repro-config` installs what the devshell needs as root before the
  shell starts; `--devshell-sudo` restores passwordless sudo (and lifts
  `no-new-privileges`, which sudo cannot work under). Other packages go
  in from the host: `bin/repro --devshell --devshell-install PKG... <cell>`,
  or `docker exec -u 0 <container> apt-get install -y PKG`. Inside,
  `apt-get`, `apt` and `sudo` are wrappers in `/usr/local/bin` that run
  the real tool and, when a package change fails, print both commands
  for this container below the tool's own error. Python packages need no
  root (a venv), nor does conda (micromamba).
- All capabilities are dropped except `CHOWN`, `DAC_OVERRIDE`,
  `FOWNER`, `FSETID`, `KILL`, `SETGID` and `SETUID` -- what the init
  script, apt and the switch to `dev` need -- and processes are limited.
- No Docker socket, and the mounts above, nothing else.
- Changing any of these re-creates the container; the restrictions it
  was created with are recorded in a label.
- Output of the non-interactive steps (`repro-config`, the init
  self-check, `--devshell-script`) reaches your terminal with every
  control sequence but colour removed, so nothing in the container can
  retitle, reprogram or write the clipboard of the terminal it runs in.
  The interactive shell is a real terminal session and is not filtered.

Not covered: the network is open (the AI needs its API, git and package
mirrors), so the container can reach what the machine can. Put the
devshell on a restricted network through `--devshell-docker-options`
if that matters for the work at hand.

### Reaching the rest of the host

`--devshell-docker-options` hands a string to the container's
`docker run` unchanged, for the host resources the hermetic default
leaves out. GPUs are what this is usually for:

```bash
bin/repro --devshell --devshell-docker-options='--gpus all' <cell>
```

Spell it with the `=`, as above: an option value that starts with a
dash reads as another flag when it is passed as a separate word.

`--gpus all` needs the NVIDIA driver plus `nvidia-container-toolkit`
on the host, registered with the daemon (`nvidia-ctk runtime
configure --runtime=docker`); check it with `docker run --rm --gpus
all ubuntu nvidia-smi` before blaming the devshell. Where the daemon
speaks CDI instead, the spelling is `--device nvidia.com/gpu=all`.
Only the driver comes in this way -- `libcuda.so`, `nvidia-smi` -- so
a cell that compiles CUDA still needs a toolkit installed inside.
Not available on macOS: Docker Desktop's VM has no GPU to pass on.

The options come last on the command line, so they beat what
`bin/repro` chose, and they are recorded as a container label:
asking for different ones re-creates the container, because docker
fixes them at creation just like the mounts.

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
#   ... inside, `claude` is already installed (repro-config puts it
#       there on entry; log in once per container). Iterate, then:
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
   consumer's `$CC --version` diverges. Also sets `sloppiness` to the
   producer's recorded value plus `pch_defines,time_macros`: LLVM >= 23
   builds with precompiled headers, which ccache refuses to cache
   without them. Sloppiness is not part of the key, so adding to it
   costs no hits.
5. **recipe host deps**: runs
   `recipes/<recipe>/devshell-setup.sh` off the read-only
   `/ci-workflows` bind, when the recipe ships one. Step 1 installs
   the LLVM set and nothing else, so any recipe with dependencies
   beyond it needs this — without it the biodynamo devshell cannot
   configure (`We did not find any OpenMPI installation`) or build a
   consumer against the artifact. Warns rather than aborts.

   The script is deliberately outside `compute_key.py`'s hashed set
   (`recipe.yaml`, the build script, `patches/**`,
   `actions/lib/**.py`): it changes what a *devshell* installs, never
   what the published artifact contains, so editing it must not
   orphan the recipe's cells. The corollary is that it duplicates
   `build.py`'s package list by hand — keep the two in step.
6. **cmake configure**: replays the recipe's own cmake invocation
   from `manifest.cmake_args`, substituting
   `CMAKE_INSTALL_PREFIX` and the source path. Pre-`cmake_args`
   manifests fall back to `llvm_build.base_cmake_args() +
   LLVM_ENABLE_PROJECTS=clang`. Failure warns rather than aborting,
   and clears the `CMakeCache.txt` cmake leaves behind on abort so a
   later session retries — a devshell whose *recipe* source is not
   configured is still a working devshell.
7. **locale**: ccache hashes `LANG`, `LC_ALL`, `LC_CTYPE` and
   `LC_MESSAGES` into every key. Replays the smoke compile below
   read-only under each candidate -- the manifest's recorded
   `build_env.ccache.locale` first, then none, `LANG=C.UTF-8`,
   `LC_CTYPE=C.UTF-8` -- and keeps the first the producer's cache
   answers. A hit only counts when the entry predates the manifest's
   `built_at`, so the devshell's own earlier compiles cannot pass for
   the producer's. The winner is written to `/etc/devshell-env.sh`,
   sourced through `BASH_ENV` (non-interactive shells, the AI's tool
   calls) and `/etc/bash.bashrc` (interactive ones). Warns when the
   recorded value is not the one that hit. `build_manifest.py` records
   the locale from its own Python process on purpose: `build.py`'s
   compiles inherit Python's PEP 538 coercion (`LC_CTYPE=C.UTF-8` when
   `LANG` is unset), and so does it.
8. **smoke compile**: builds
   `lib/Support/CMakeFiles/LLVMSupport.dir/Allocator.cpp.o`. A miss
   on the producer's entries ⇒ its cache isn't reaching the consumer
   (drift the earlier checks didn't catch); surfaces a `::warning::`
   rather than aborting.

### Limits

- Linux Ubuntu cells only (`ubuntu-22.04`, `ubuntu-24.04`); other
  cell OSes refuse with a clear error.
- macOS hosts work via the Linux container, paying Rosetta
  emulation overhead on Apple Silicon.
- Pre-portable-ccache manifests (no `build_env.ccache`) provision
  correctly but miss on the first compile until a republish writes
  the portable-hashing config alongside.
