#!/bin/sh
set -eu
destination="${1:-/opt/pageindex}"
revision="190f8b378be58199ca993566a9214dba72089c54"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
patch_file="${script_dir}/../patches/pageindex-runtime.patch"
git clone --filter=blob:none https://github.com/VectifyAI/PageIndex.git "$destination"
git -C "$destination" checkout "$revision"
git -C "$destination" apply --check "$patch_file"
git -C "$destination" apply "$patch_file"
