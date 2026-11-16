#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in t1|t3) ;; *) printf '%s\n' 'Expected t1 or t3.' >&2; exit 2 ;; esac
: "${BRIDGE_SIGNAL_DIR:?Set BRIDGE_SIGNAL_DIR to a new, private absolute directory}"
if [[ $BRIDGE_SIGNAL_DIR != /* || ! -d $BRIDGE_SIGNAL_DIR ]]; then
    printf '%s\n' 'BRIDGE_SIGNAL_DIR must be an existing absolute directory.' >&2
    exit 2
fi
ready="$BRIDGE_SIGNAL_DIR/$1.ready"
done_file="$BRIDGE_SIGNAL_DIR/$1.done"
if [[ -e $ready || -L $ready || -e $done_file || -L $done_file ]]; then
    printf '%s\n' 'Signal files already exist; use a fresh directory.' >&2
    exit 2
fi
umask 077
set -C
: > "$ready"
set +C
printf 'READY_FOR_BRIDGE: %s; release with: touch %s\n' "$1" "$done_file"
for attempt in {1..240}; do
    if [[ -f $done_file ]]; then exit 0; fi
    sleep 1
done
printf '%s\n' 'Timed out waiting for investigation; restoring fault.' >&2
exit 1
