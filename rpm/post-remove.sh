#!/bin/bash
# $1 = 0 on final uninstall (not upgrade)
if [ "$1" = "0" ]; then
    rm -rf /opt/venvs/junos-ops
fi
