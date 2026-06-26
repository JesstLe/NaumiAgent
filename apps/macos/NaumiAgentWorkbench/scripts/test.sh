#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

frameworks="/Library/Developer/CommandLineTools/Library/Developer/Frameworks"
tool_libs="/Library/Developer/CommandLineTools/Library/Developer/usr/lib"

if [[ -d "$frameworks" && -d "$tool_libs" ]]; then
  swift test \
    -Xswiftc -F -Xswiftc "$frameworks" \
    -Xlinker -rpath -Xlinker "$frameworks" \
    -Xlinker -rpath -Xlinker "$tool_libs"
else
  swift test
fi
