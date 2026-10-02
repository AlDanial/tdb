#!/usr/bin/env bash
total=0
for ((o=0; o<2; o++)); do
    total=$((total + 1))
    for ((i=0; i<2; i++)); do
        total=$((total + 10))
    done
done
echo "total=$total"
