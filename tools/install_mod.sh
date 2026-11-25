#!/usr/bin/env bash
# Copy the shared box mod into the NMS.py mods folder on this PC and install its dependency.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODS="${NMS_MODS_DIR:-/mnt/c/Users/computer/NMSpyMods}"
PY="${NMS_PYTHON:-/mnt/c/Users/computer/AppData/Local/Programs/Python/Python311/python.exe}"
if [ -f "$MODS/shared_box_spike.py" ]; then
  mkdir -p "$REPO/spike"
  mv "$MODS/shared_box_spike.py" "$REPO/spike/"
  echo "Moved the spike mod out of $MODS (it hooks the same function)."
fi
mkdir -p "$MODS/_sharedbox/sharedbox_lib"
cp "$REPO/mod/shared_box.py" "$REPO/mod/start-nms-shared.bat" "$MODS/"
cp "$REPO/mod/sharedbox_lib/"*.py "$MODS/_sharedbox/sharedbox_lib/"
[ -f "$MODS/shared_box.toml" ] || cp "$REPO/mod/shared_box.toml.example" "$MODS/shared_box.toml"
"$PY" -m pip install --quiet websockets==13.1
echo "Installed into $MODS. Fill in shared_box.toml if you haven't."
