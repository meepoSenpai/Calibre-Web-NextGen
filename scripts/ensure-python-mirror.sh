#!/usr/bin/env bash
# Ensure our GHCR mirrors of the build deps that come from the GitHub release CDN
# exist. Currently: python-build-standalone (Python interpreter) and kepubify.
#
# WHY THIS EXISTS
#   The GitHub release-asset CDN intermittently 404s the GitHub-Actions egress —
#   sometimes for many minutes (proven 2026-06-28) — which broke every image
#   build at these downloads. We mirror them into our own GHCR repo and the
#   Dockerfile COPYs them from there. GHCR is as reliable as the base-image pulls.
#
# WHAT THIS DOES (idempotent — safe to run every build)
#   Reads the version pins from the Dockerfile (single source of truth), and for
#   each mirror: if its GHCR tag already exists -> nothing to do; otherwise builds
#   a tiny multi-arch image holding the artifact and pushes it.
#
# >>> TO BUMP PYTHON or KEPUBIFY: change ONLY the ARG in the Dockerfile. <<<
#   The Dockerfile mirror stages and this script both derive the tags from those
#   ARGs, and CI runs this before every build, so the new mirror is built once
#   automatically. No other manual step.
#
# AUTH: set GHCR_TOKEN to a token with write:packages (CI passes secrets.GH_PAT).
#
# OWNER: the mirror lives in the namespace of the repository running the build
# (GITHUB_REPOSITORY_OWNER in Actions), so a fork maintains its own copy. Image
# builds must pass the same ghcr.io/<owner>/pbs-cache as the PBS_MIRROR arg.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE="${REPO_ROOT}/Dockerfile"
OWNER="${GITHUB_REPOSITORY_OWNER:-new-usemame}"
GHCR_USER="${GHCR_USER:-${OWNER}}"
# Registry repository names must be lowercase; GitHub owners need not be.
MIRROR_PATH="$(printf '%s' "${OWNER}" | tr '[:upper:]' '[:lower:]')/pbs-cache"   # ghcr.io/<this>
MIRROR_REPO="ghcr.io/${MIRROR_PATH}"

read_arg() { grep -E "^ARG ${1}=" "$DOCKERFILE" | head -1 | cut -d= -f2; }
PYTHON_VERSION="$(read_arg PYTHON_VERSION)"
PBS_RELEASE="$(read_arg PYTHON_BUILD_STANDALONE_RELEASE)"
KEPUBIFY_RELEASE="$(read_arg KEPUBIFY_RELEASE)"
[ -n "$PYTHON_VERSION" ] && [ -n "$PBS_RELEASE" ] && [ -n "$KEPUBIFY_RELEASE" ] \
  || { echo "ERROR: could not read version pins from $DOCKERFILE"; exit 1; }

# Authenticated existence check — the package is private, so an anonymous token
# can't see it and would always report "missing" (needless rebuild).
mirror_exists() {  # mirror_exists <tag>
  local tag="$1" tok
  if [ -n "${GHCR_TOKEN:-}" ]; then
    tok="$(curl -s -u "${GHCR_USER}:${GHCR_TOKEN}" "https://ghcr.io/token?scope=repository:${MIRROR_PATH}:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  else
    tok="$(curl -s "https://ghcr.io/token?scope=repository:${MIRROR_PATH}:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  fi
  curl -sf -o /dev/null -H "Authorization: Bearer ${tok}" \
    -H 'Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json' \
    "https://ghcr.io/v2/${MIRROR_PATH}/manifests/${tag}"
}

ensured_login=0
ensure_login() {
  [ "$ensured_login" = "1" ] && return 0
  : "${GHCR_TOKEN:?GHCR_TOKEN (token with write:packages, e.g. GH_PAT) required to build a mirror}"
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
  docker buildx create --use --name pbs-mirror-builder >/dev/null 2>&1 || docker buildx use pbs-mirror-builder
  ensured_login=1
}

dl() {  # dl <url> <out>
  echo "  downloading $1"
  curl -fL --connect-timeout 30 --retry 8 --retry-delay 5 --retry-all-errors -o "$2" "$1"
}

# ── Mirror 1: python-build-standalone tarball -> /python.tar.gz ───────────────
PY_TAG="cpython-${PYTHON_VERSION}-${PBS_RELEASE}"
if mirror_exists "$PY_TAG"; then
  echo "Python mirror ${MIRROR_REPO}:${PY_TAG} present."
else
  echo "Building Python mirror ${MIRROR_REPO}:${PY_TAG}"
  ensure_login
  W="$(mktemp -d)"
  dl "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PYTHON_VERSION}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"  "${W}/python-amd64.tar.gz"
  dl "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PYTHON_VERSION}+${PBS_RELEASE}-aarch64-unknown-linux-gnu-install_only.tar.gz" "${W}/python-arm64.tar.gz"
  printf '# syntax=docker/dockerfile:1\nFROM scratch\nARG TARGETARCH\nCOPY python-${TARGETARCH}.tar.gz /python.tar.gz\n' > "${W}/Dockerfile"
  docker buildx build --platform linux/amd64,linux/arm64 -t "${MIRROR_REPO}:${PY_TAG}" --push "${W}"
  rm -rf "${W}"
fi

# ── Mirror 2: kepubify binary -> /kepubify (0755) ─────────────────────────────
KP_TAG="kepubify-${KEPUBIFY_RELEASE}"
if mirror_exists "$KP_TAG"; then
  echo "kepubify mirror ${MIRROR_REPO}:${KP_TAG} present."
else
  echo "Building kepubify mirror ${MIRROR_REPO}:${KP_TAG}"
  ensure_login
  W="$(mktemp -d)"
  dl "https://github.com/pgaskin/kepubify/releases/download/${KEPUBIFY_RELEASE}/kepubify-linux-64bit" "${W}/kepubify-amd64"
  dl "https://github.com/pgaskin/kepubify/releases/download/${KEPUBIFY_RELEASE}/kepubify-linux-arm64" "${W}/kepubify-arm64"
  printf '# syntax=docker/dockerfile:1\nFROM scratch\nARG TARGETARCH\nCOPY --chmod=755 kepubify-${TARGETARCH} /kepubify\n' > "${W}/Dockerfile"
  docker buildx build --platform linux/amd64,linux/arm64 -t "${MIRROR_REPO}:${KP_TAG}" --push "${W}"
  rm -rf "${W}"
fi

echo "All build-dep mirrors present."
