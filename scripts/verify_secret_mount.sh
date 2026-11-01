#!/usr/bin/env bash
# Evidence probe for the secret bind-mount permission rule documented in
# deploy/docker-compose.yml and docs/security.md.
#
# Claim being checked: when a Compose secret uses the `file:` driver, Compose
# delivers it as a bind mount. Bind mounts do not remap ownership or mode, so
# the file arrives in the container with the *host* file's uid/gid and mode.
# Therefore a root-owned 0600 secret is unreadable by the container user
# (uid/gid 10001), while `chown 10001:10001 && chmod 0400` is readable.
#
# This script needs Docker and a writable scratch dir. It creates its own
# throwaway secret files under the workspace and never touches real secrets.
# It is a manual verification aid, not part of the pytest suite (the suite
# asserts the *artifacts* are consistent with this rule; only a live engine can
# prove the rule itself).
#
# Usage: scripts/verify_secret_mount.sh
set -uo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "SKIP: docker is not installed; cannot verify the live bind-mount rule." >&2
  exit 0
fi

WORK="${BH_AGENT_WORKSPACE:-${TMPDIR:-/tmp}}/verify_secret_mount_$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

IMG="python:3.11.15-slim-bookworm"
# The runtime image user; matches `user: "10001:10001"` in the compose file.
readonly RUN_UID=10001

root_secret="$WORK/root_0600"
owned_secret="$WORK/owned_0400"
printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' > "$root_secret"
printf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n' > "$owned_secret"
chmod 0600 "$root_secret"
# chown needs root; the agent runs as a normal user on the host, so use a
# container to set ownership that 10001 can read.
docker run --rm -v "$WORK":/w "$IMG" \
  sh -c "chown ${RUN_UID}:${RUN_UID} /w/owned_0400 && chmod 0400 /w/owned_0400" >/dev/null

echo "--- host files ---"
ls -n "$WORK"/*

probe() {
  local path="$1" label="$2"
  local out
  out="$(docker run --rm --user "${RUN_UID}:${RUN_UID}" -v "$path":/secret:ro "$IMG" \
        python -c 'import sys; print(open("/secret","rb").read()[:1] == b"a")' 2>&1)"
  echo "$label (uid ${RUN_UID}) can read: $out"
}

echo "--- container read as uid ${RUN_UID} ---"
probe "$root_secret" "root:0600"
probe "$owned_secret" "10001:0400"

echo
echo "Expected: root:0600 -> False (PermissionError); 10001:0400 -> True."
echo "If that is what you see, the rule in the compose header is correct."