#!/bin/sh
# Container isolation replaces Chromium's unavailable setuid/user-namespace sandbox.
# GENOFFICE_APP_BIN selects this wrapper only for hidden export processes.
unset ELECTRON_RUN_AS_NODE
exec /opt/GenOffice/genoffice --no-sandbox "$@"
