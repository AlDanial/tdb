#!/bin/tcsh -f
echo -n "enter: "
set x = $<
if ("$x" == "") then
    echo "eof"
else
    echo "got:$x"
endif
