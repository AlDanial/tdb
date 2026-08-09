total=0
for i in 1 2 3 4 5; do
    total=$((total + i))
done
echo "total=$total"
sleep_done=""
for j in 1 2 3 4 5 6 7 8 9 10; do
    builtin sleep 0.1 2>/dev/null || sleep 0.1
    sleep_done="$sleep_done$j,"
done
echo "slept"
