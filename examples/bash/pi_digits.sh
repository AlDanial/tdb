#!/usr/bin/env bash
set -euo pipefail
runs=1
count=10
print_digits=0
while (($#)); do
    case "$1" in
        -r|--n-runs) runs=$2; shift 2 ;;
        -d|--n-digits) count=$2; shift 2 ;;
        -p|--print) print_digits=1; shift ;;
        *) echo "usage: $0 [-r runs] [-d digits] [-p]" >&2; exit 2 ;;
    esac
done
if ! [[ $runs =~ ^[1-9][0-9]*$ && $count =~ ^[1-9][0-9]*$ ]]; then
    echo 'counts must be positive integers' >&2
    exit 2
fi

pi_digits() {
    local count=$1 size=$((count * 10 / 3 + 1))
    local -a a=()
    local i step q x previous=0 pending=0 out=''
    for ((i=0; i<size; i++)); do a[i]=2; done
    for ((step=0; step<=count; step++)); do
        q=0
        for ((i=size; i>=1; i--)); do
            x=$((10 * a[i-1] + q * i))
            a[i-1]=$((x % (2*i-1)))
            q=$((x / (2*i-1)))
        done
        a[0]=$((q % 10))
        q=$((q / 10))
        if ((q == 9)); then
            ((pending+=1))
        elif ((q == 10)); then
            out+=$((previous+1))
            while ((pending > 0)); do out+=0; pending=$((pending - 1)); done
            previous=0
        else
            out+=$previous
            while ((pending > 0)); do out+=9; pending=$((pending - 1)); done
            previous=$q
        fi
    done
    out+=$previous
    while ((pending > 0)); do out+=9; pending=$((pending - 1)); done
    REPLY=${out:1:count}
}

for ((run=1; run<=runs; run++)); do
    start=$(date +%s%N)
    pi_digits "$count"
    end=$(date +%s%N)
    printf 'Run %3d/%3d completed in %d.%06d seconds.\n' "$run" "$runs" "$(((end-start)/1000000000))" "$((((end-start)%1000000000)/1000))"
    if ((run == runs && print_digits)); then printf '%s\n' "$REPLY"; fi
done
