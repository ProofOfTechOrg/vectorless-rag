#!/bin/sh
set -eu
destination="${1:-/opt/pageindex}"
revision="190f8b378be58199ca993566a9214dba72089c54"
git clone --filter=blob:none https://github.com/VectifyAI/PageIndex.git "$destination"
git -C "$destination" checkout "$revision"

