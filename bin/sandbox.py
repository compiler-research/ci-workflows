"""Every docker invocation bin/repro makes, in one place.

bin/repro used to assemble `docker run` / `exec` / `cp` / `inspect`
argument lists at each call site. They all live here now, so what a
container is allowed to see and do can be reviewed in one file:

  - DevshellSpec    the long-lived devshell container: image, platform,
                    every mount (with its access mode), environment,
                    labels, and the caller's own --devshell-docker-options
  - the helpers     one-shot containers, exec, cp, inspect, rm

Nothing else in bin/ builds a docker command line; test_sandbox.py pins
the argv each function produces, so a change to what a container gets
shows up as a test diff.

Stdlib-only, like the rest of bin/.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

DOCKER = "docker"

# Docker Desktop's CLI ends interactive sessions with "What's next:"
# hints (e.g. to try `docker debug` on the container). They are noise in
# a devshell, and point at a way into the container that bypasses its
# restrictions; export DOCKER_CLI_HINTS=true to have them back.
os.environ.setdefault("DOCKER_CLI_HINTS", "false")


@dataclass(frozen=True)
class Mount:
    """A bind or named-volume mount. `source` is a host path or a volume
    name, as `docker run -v` takes it."""
    source: str
    target: str
    readonly: bool = False

    def argv(self) -> List[str]:
        return ["-v", f"{self.source}:{self.target}"
                      + (":ro" if self.readonly else "")]


@dataclass
class DevshellSpec:
    """Everything `docker run` is told about the devshell container."""
    name: str
    image: str
    platform: str
    workdir: str
    mounts: List[Mount]
    env: List[Tuple[str, str]]
    labels: Dict[str, str] = field(default_factory=dict)
    #: Container-level restrictions (capabilities, security options,
    #: limits). Kept as their own field so they read as one block.
    security: List[str] = field(default_factory=list)
    #: --devshell-docker-options, verbatim and last, so a caller can
    #: override what repro chose.
    caller_options: str = ""
    command: Sequence[str] = ("sleep", "infinity")

    def run_argv(self) -> List[str]:
        argv = [DOCKER, "run", "-d", "--platform", self.platform,
                "--name", self.name]
        for m in self.mounts:
            argv += m.argv()
        argv += ["-w", self.workdir]
        for k, v in self.env:
            argv += ["-e", f"{k}={v}"]
        for k, v in self.labels.items():
            argv += ["--label", f"{k}={v}"]
        argv += list(self.security)
        argv += shlex.split(self.caller_options)
        argv += [self.image, *self.command]
        return argv

    def bind_map(self) -> Dict[str, str]:
        """destination -> source, as container_binds() reports them."""
        return {m.target: m.source for m in self.mounts}


def run_detached(spec: DevshellSpec) -> None:
    subprocess.run(spec.run_argv(), check=True, stdout=subprocess.DEVNULL)


def oneshot(image: str, command: Sequence[str], *,
            mounts: Sequence[Mount] = (), platform: Optional[str] = None,
            capture: bool = False, check: bool = False
            ) -> subprocess.CompletedProcess:
    """`docker run --rm` of a throwaway container."""
    argv = [DOCKER, "run", "--rm"]
    if platform:
        argv += ["--platform", platform]
    for m in mounts:
        argv += m.argv()
    argv += [image, *command]
    return subprocess.run(argv, check=check,
                          capture_output=capture,
                          stdout=None if capture else subprocess.DEVNULL)


def exec_argv(name: str, command: Sequence[str], *,
              user: Optional[str] = None, workdir: Optional[str] = None,
              tty: bool = False,
              env: Sequence[Tuple[str, str]] = ()) -> List[str]:
    argv = [DOCKER, "exec"]
    if tty:
        argv.append("-it")
    if user is not None:
        argv += ["-u", user]
    if workdir is not None:
        argv += ["-w", workdir]
    for k, v in env:
        argv += ["-e", f"{k}={v}"]
    return argv + [name, *command]


#: Terminal control sequences a container's output may carry. Colour
#: (CSI ... m) is kept; everything else -- OSC (window title, clipboard,
#: hyperlinks), DCS and friends, cursor and mode control, bare C0/C1
#: controls -- is dropped before it reaches the user's terminal, which
#: would otherwise act on it.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_STRING = re.compile(r"\x1b[P^_X].*?(?:\x1b\\|$)", re.S)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*([@-~])")
#: Any other escape: ESC, optional intermediates, a final byte (ESC c,
#: ESC 7, ESC ( B, ...). Kept colour CSI is excluded by the lookahead.
_ESC = re.compile(r"\x1b(?!\[[0-?]*[ -/]*m)(?:[ -/]*[0-~])?")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f\x80-\x9f]")


def clean_terminal_output(text: str) -> str:
    """`text` with every control sequence but colour removed."""
    text = _OSC.sub("", text)
    text = _STRING.sub("", text)
    text = _CSI.sub(lambda m: m.group(0) if m.group(1) == "m" else "", text)
    text = _ESC.sub("", text)
    return _CTRL.sub("", text)


def exec_(name: str, command: Sequence[str], **kw) -> subprocess.CompletedProcess:
    """Run `command` in a running container. `capture`/`check`/`quiet`
    control the subprocess; the rest go to exec_argv.

    Output that is neither captured nor discarded is streamed to this
    process's stdout through clean_terminal_output: what the container
    prints is not the container's to put on the user's terminal as-is.
    """
    capture = kw.pop("capture", False)
    check = kw.pop("check", False)
    quiet = kw.pop("quiet", False)
    argv = exec_argv(name, command, **kw)
    if capture or quiet:
        return subprocess.run(
            argv, check=check, text=True, capture_output=capture,
            stdout=subprocess.DEVNULL if quiet and not capture else None)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        sys.stdout.write(clean_terminal_output(
            raw.decode("utf-8", errors="replace")))
        sys.stdout.flush()
    proc.stdout.close()
    rc = proc.wait()
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, argv)
    return subprocess.CompletedProcess(argv, rc)


def cp_in(src: str, name: str, dest: str) -> None:
    subprocess.run([DOCKER, "cp", src, f"{name}:{dest}"],
                   check=True, stdout=subprocess.DEVNULL)


def rm(name: str, *, check: bool = True, quiet_errors: bool = False) -> None:
    subprocess.run([DOCKER, "rm", "-f", name], check=check,
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL if quiet_errors else None)


def start(name: str, *, check: bool = True, quiet_errors: bool = False) -> None:
    subprocess.run([DOCKER, "start", name], check=check,
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL if quiet_errors else None)


def _inspect(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([DOCKER, *argv], capture_output=True, text=True)


def container_exists(name: str) -> bool:
    return _inspect("container", "inspect", name).returncode == 0


def container_running(name: str) -> bool:
    r = _inspect("container", "inspect", "-f", "{{.State.Running}}", name)
    return r.returncode == 0 and r.stdout.strip() == "true"


def container_binds(name: str) -> Dict[str, str]:
    """destination -> source for an existing container's mounts. A
    volume reports its name as the source; a bind, its host path."""
    r = _inspect("inspect", name, "--format",
                 '{{range .Mounts}}{{.Destination}}\t'
                 '{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}'
                 '\n{{end}}')
    if r.returncode != 0:
        return {}
    binds = {}
    for line in r.stdout.splitlines():
        dst, _, src = line.partition("\t")
        if dst:
            binds[dst] = src
    return binds


def container_label(name: str, label: str) -> str:
    """A label's value, "" when absent (Go prints `<no value>`)."""
    r = _inspect("inspect", name, "--format",
                 '{{index .Config.Labels "' + label + '"}}')
    if r.returncode != 0:
        return ""
    out = r.stdout.strip()
    return "" if out == "<no value>" else out


def container_arch(name: str) -> str:
    """Architecture of the image a container was created from, or ""."""
    r = _inspect("inspect", name, "--format", "{{.Image}}")
    if r.returncode != 0 or not r.stdout.strip():
        return ""
    r = _inspect("image", "inspect", r.stdout.strip(),
                 "--format", "{{.Architecture}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def volume_exists(name: str) -> bool:
    return _inspect("volume", "inspect", name).returncode == 0


def volume_create(name: str) -> None:
    subprocess.run([DOCKER, "volume", "create", name],
                   check=True, stdout=subprocess.DEVNULL)


def latest_container(name_filter: str) -> Optional[str]:
    """ID of the most recent container whose name matches, or None."""
    r = subprocess.run([DOCKER, "ps", "-aq", "--filter",
                        f"name={name_filter}", "--latest"],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip() or None
