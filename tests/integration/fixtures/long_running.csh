#!/usr/bin/tcsh -f
/bin/sh -c 'trap "" TERM; exec /bin/sleep 30' &
echo $! > "$argv[1]"
echo "long running ready"
wait
