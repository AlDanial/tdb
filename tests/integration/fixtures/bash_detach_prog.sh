#!/usr/bin/env bash
mode="${1:-}"
echo "before-$(echo ran)"
x=1  # breakpoint line
echo "after-$(echo ran)"
if [ "$mode" = trap ]; then
    trap 'echo "caught-$(echo int)"; exit 5' INT
fi
if [ "$mode" = loop ] || [ "$mode" = trap ]; then
    echo "looping-$(echo now)"
    while true; do sleep 0.2; done
fi
read -r line
echo "got:$line"
exit 7
