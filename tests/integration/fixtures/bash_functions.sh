inner() {
    local iv=99
    echo "inner"
}
outer() {
    inner
    echo "outer after inner"
}
outer
echo "top after outer"
