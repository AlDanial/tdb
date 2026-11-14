#!/bin/tcsh -f
set mode = ""
if ($#argv > 0) then
    set mode = "$argv[1]"
endif
echo before-`echo ran`
set x = 1
echo after-`echo ran`
if ("$mode" == trap) onintr caught
if ("$mode" == ignore) onintr -
if ("$mode" == loop || "$mode" == trap || "$mode" == ignore) then
    echo looping-`echo now`
    set n = 0
    while ($n < 40)
        sleep 0.2
        @ n++
    end
    if ("$mode" == ignore) then
        echo survived-`echo int`
        exit 6
    endif
    exit 9
endif
if ("$mode" == read) echo looping-`echo now`
set line = "$<"
echo got:$line
exit 7
caught:
echo caught-`echo int`
exit 5
