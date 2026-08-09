value=$(echo "from-subshell")
(echo "in explicit subshell")
echo "one" | while read -r w; do echo "piped $w"; done
echo "value=$value"
