cat << 'EOF'
hello $user
EOF
echo "a # remains text"
cat << EOF
second body
EOF
source `command`
source *.csh