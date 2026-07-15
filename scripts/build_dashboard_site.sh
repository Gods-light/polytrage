#!/bin/sh
# Wrap dashboard/index.html (artifact-flavored: no doctype/html/body — the
# Artifact platform adds those) into a complete HTML document for nginx at
# dashboard/site/index.html, served as https://polytrage.logicflow.co.il.
# Run after every dashboard/index.html change; nginx bind-mounts the dir,
# so no container restart is needed.
set -eu
cd "$(dirname "$0")/.."
mkdir -p dashboard/site
{
  printf '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
  cat dashboard/index.html
  printf '\n</body>\n</html>\n'
} > dashboard/site/index.html
echo "built dashboard/site/index.html ($(wc -c < dashboard/site/index.html) bytes)"
