set -euo pipefail
count=0
bump() {
    local n=$1
    count=$((count + n))
}
bump 2
bump 3
echo "count=$count"
