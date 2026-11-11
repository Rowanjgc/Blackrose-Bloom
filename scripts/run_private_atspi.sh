#!/usr/bin/env bash
# Run a native GUI probe with a private Xvfb and AT-SPI bus/registry.
# Usage: dbus-run-session -- scripts/run_private_atspi.sh command [args...]
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: $0 command [args...]" >&2
  exit 2
fi

if [[ -n "${BLACKROSE_XVFB:-}" ]]; then
  xvfb=$BLACKROSE_XVFB
elif [[ -x /tmp/blackrose-h07-xvfb/root/usr/bin/Xvfb ]]; then
  xvfb=/tmp/blackrose-h07-xvfb/root/usr/bin/Xvfb
else
  xvfb=$(command -v Xvfb || true)
fi
if [[ ! -x "$xvfb" ]]; then
  echo "Xvfb not found; set BLACKROSE_XVFB to an executable path" >&2
  exit 2
fi

scratch=$(mktemp -d "${TMPDIR:-/tmp}/blackrose-private-atspi-XXXXXX")
xvfb_pid=
launcher_pid=
registry_pid=
cleanup() {
  for pid in "$registry_pid" "$launcher_pid" "$xvfb_pid"; do
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  done
  for pid in "$registry_pid" "$launcher_pid" "$xvfb_pid"; do
    if [[ -n "$pid" ]]; then wait "$pid" 2>/dev/null || true; fi
  done
  rm -rf "$scratch"
}
trap cleanup EXIT INT TERM

"$xvfb" -displayfd 3 -screen 0 1280x900x24 -nolisten tcp \
  3>"$scratch/display" >"$scratch/xvfb.log" 2>&1 &
xvfb_pid=$!
for _ in {1..100}; do
  [[ -s "$scratch/display" ]] && break
  kill -0 "$xvfb_pid" 2>/dev/null || { cat "$scratch/xvfb.log" >&2; exit 1; }
  sleep .05
done
[[ -s "$scratch/display" ]] || { echo "Xvfb did not become ready" >&2; exit 1; }

export DISPLAY=":$(<"$scratch/display")" GDK_BACKEND=x11 WEBKIT_DISABLE_DMABUF_RENDERER=1
unset WAYLAND_DISPLAY || true

/usr/libexec/at-spi-bus-launcher --launch-immediately --a11y=1 --screen-reader=1 \
  >"$scratch/launcher.log" 2>&1 &
launcher_pid=$!
for _ in {1..50}; do
  address=$(dbus-send --session --print-reply --dest=org.a11y.Bus \
    /org/a11y/bus org.a11y.Bus.GetAddress 2>/dev/null | awk -F '"' '/string/ {print $2; exit}' || true)
  [[ -n "$address" ]] && break
  kill -0 "$launcher_pid" 2>/dev/null || { cat "$scratch/launcher.log" >&2; exit 1; }
  sleep .05
done
[[ -n "${address:-}" ]] || { echo "AT-SPI bus address unavailable" >&2; cat "$scratch/launcher.log" >&2; exit 1; }
export AT_SPI_BUS_ADDRESS="$address"

/usr/libexec/at-spi2-registryd >"$scratch/registry.log" 2>&1 &
registry_pid=$!
sleep .1
export BLACKROSE_PRIVATE_ATSPI=1
"$@"
