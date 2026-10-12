#!/usr/bin/env bash
# Build the fbchat-v2 E2EE bridge against a current mautrix-meta.
#
# The prebuilt bridge shipped with fbchat-v2 2.3.2 pins a mautrix-meta from
# May 2026, which can no longer sync the inbox (Messenger moved its main socket
# to DGW), so the connection never becomes ready. This rebuilds the same bridge
# source against a newer mautrix-meta with the small port in contrib/bridge/.
#
# Requires git and Go (any recent version; the needed toolchain is fetched).
set -euo pipefail

FBCHAT_TAG="v2.3.2"
META_COMMIT="080ba2d3d5e9d2e8b35ea32170289b2512b548fe"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
patch_file="$repo_root/contrib/bridge/fbchat-v2-2.3.2-mautrix-meta.patch"
data_dir="${MESSENGER_MCP_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/messenger-mcp}"
output="${1:-$data_dir/bin/fbchat-bridge-e2ee}"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git clone --quiet --depth 1 --branch "$FBCHAT_TAG" https://github.com/m008v/fbchat-v2 "$work/fbchat-v2"
cd "$work/fbchat-v2"
git clone --quiet https://github.com/mautrix/meta bridge-e2ee/meta
git -C bridge-e2ee/meta -c advice.detachedHead=false checkout --quiet "$META_COMMIT"
git apply "$patch_file"

cd bridge-e2ee
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod
mkdir -p "$(dirname "$output")"
go build -trimpath -ldflags="-s -w" -o "$output.tmp" .
chmod 700 "$output.tmp"
mv "$output.tmp" "$output"
echo "Built $output"
