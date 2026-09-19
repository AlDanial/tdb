#!/usr/bin/env tcsh
set runs = 1
set count = 10
set print_digits = 0
while ( $#argv > 0 )
    switch ( "$argv[1]" )
    case -r:
    case --n-runs:
        if ( $#argv < 2 ) goto usage
        set runs = "$argv[2]"
        shift argv
        shift argv
        breaksw
    case -d:
    case --n-digits:
        if ( $#argv < 2 ) goto usage
        set count = "$argv[2]"
        shift argv
        shift argv
        breaksw
    case -p:
    case --print:
        set print_digits = 1
        shift argv
        breaksw
    default:
        goto usage
    endsw
end
if ( "$runs" !~ [1-9]* || "$count" !~ [1-9]* ) goto usage
@ run = 1
while ( $run <= $runs )
    set start = `date +%s%N`
    @ size = $count * 10 / 3 + 1
    set a = ()
    @ i = 1
    while ( $i <= $size )
        set a = ( $a 2 )
        @ i++
    end
    set out = ""
    @ previous = 0
    @ pending = 0
    @ step = 0
    while ( $step <= $count )
        @ q = 0
        @ i = $size
        while ( $i >= 1 )
            @ x = 10 * $a[$i] + $q * $i
            @ a[$i] = $x % ( 2 * $i - 1 )
            @ q = $x / ( 2 * $i - 1 )
            @ i--
        end
        @ a[1] = $q % 10
        @ q = $q / 10
        if ( $q == 9 ) then
            @ pending++
        else if ( $q == 10 ) then
            @ digit = $previous + 1
            set out = "${out}${digit}"
            while ( $pending > 0 )
                set out = "${out}0"
                @ pending--
            end
            @ previous = 0
        else
            set out = "${out}${previous}"
            while ( $pending > 0 )
                set out = "${out}9"
                @ pending--
            end
            @ previous = $q
        endif
        @ step++
    end
    set out = "${out}${previous}"
    while ( $pending > 0 )
        set out = "${out}9"
        @ pending--
    end
    set digits = `echo "$out" | cut -c 2-`
    set digits = `echo "$digits" | cut -c 1-$count`
    set end = `date +%s%N`
    awk -v run="$run" -v runs="$runs" -v delta="$end" -v start="$start" 'BEGIN { printf "Run %3d/%3d completed in %.6f seconds.\n", run, runs, (delta-start)/1000000000 }'
    if ( $run == $runs && $print_digits ) echo "$digits"
    @ run++
end
exit 0
usage:
echo "usage: $0 [-r runs] [-d digits] [-p]" >&2
exit 2
