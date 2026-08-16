#!/usr/bin/env bash
# Host packages a devshell needs for this recipe. scripts/repro-config
# runs this as root inside the container. Without it a biodynamo
# devshell can neither configure BioDynaMo ("We did not find any
# OpenMPI installation") nor build a consumer against the artifact.
#
# Duplicates build.py's _install_deps by hand, and has to: that list is
# hashed into the cache key and this one must not be. See the guide's
# "What scripts/repro-config does on entry". Keep the two in step.

set -e

# See build.py's _APT_ENV: NEEDRESTART_MODE=l makes needrestart list
# services needing a restart instead of restarting them. At its default
# it bounces systemd-networkd partway through an apt run.
apt_env=(env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l)

"${apt_env[@]}" apt-get update -qq
"${apt_env[@]}" apt-get install -y --no-install-recommends \
  cmake make g++ gcc git wget curl \
  libblas-dev liblapack-dev libnuma-dev libomp-dev libopenmpi-dev \
  libpthread-stubs0-dev zlib1g-dev freeglut3-dev libbz2-dev libffi-dev \
  liblzma-dev libreadline-dev libsqlite3-dev libssl-dev tk-dev xz-utils \
  libgit2-dev \
  g++-11

# python3.9 is in no Ubuntu LTS, and BioDynaMo requires the exact
# CPython minor ROOT built its bindings against -- configure stops at
# "provides Python bindings for 3.9, but no such interpreter was found".
# Skipped when a 3.9 is already present so re-entering a shell is cheap.
if ! command -v python3.9 >/dev/null 2>&1; then
  "${apt_env[@]}" apt-get install -y --no-install-recommends \
    software-properties-common
  "${apt_env[@]}" add-apt-repository -y ppa:deadsnakes/ppa
  "${apt_env[@]}" apt-get update -qq
  "${apt_env[@]}" apt-get install -y --no-install-recommends \
    python3.9 python3.9-dev
fi
