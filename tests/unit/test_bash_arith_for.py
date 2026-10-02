"""arith_for_fixups: static detection of bash's nested arithmetic-for
line-number bug (an outer `for ((...))` whose body contains another
`for ((...))` reports the *inner* loop's line in $LINENO/BASH_LINENO)."""

from tdb.adapters.bash.arith_for import arith_for_fixups, expr_pattern


def test_expr_pattern_escapes_and_wildcards():
    assert expr_pattern("step<=count") == r"step\<\=count"
    assert expr_pattern("  i<$n ") == r"i\<*"  # expansion -> *, ws run -> *
    assert expr_pattern("c < ${n}") == r"c*\<*"
    assert expr_pattern("") == "1"  # empty expression executes as ((1))
    assert expr_pattern("x=$((a+1))") == r"x\=*"
    assert expr_pattern("x=$(f a)") == r"x\=*"
    assert expr_pattern("n=$#") == r"n\=*"


def test_no_nesting_no_fixups():
    src = "for ((i=0; i<3; i++)); do\n  :\ndone\nfor ((j=0;j<2;j++)); do :; done\n"
    assert arith_for_fixups(src) == {}


def test_outer_loop_reported_at_last_nested_header():
    src = (
        "for ((o=0; o<1; o++)); do\n"  # 1 -> reported at 6
        "    :\n"
        "    for ((i=0; i<1; i++)); do\n"  # 3
        "        :\n"
        "    done\n"
        "    for ((j=0; j<1; j++)); do\n"  # 6
        "        :\n"
        "    done\n"
        "done\n"
    )
    fix = arith_for_fixups(src)
    assert list(fix) == [6]
    assert fix[6] == [
        # the loop literally at line 6 comes first, so identical headers
        # resolve to the literal line
        (6, r"\(\(j\=0\)\)"),
        (6, r"\(\(j\<1\)\)"),
        (6, r"\(\(j\+\+\)\)"),
        (1, r"\(\(o\=0\)\)"),
        (1, r"\(\(o\<1\)\)"),
        (1, r"\(\(o\+\+\)\)"),
    ]


def test_triple_nesting_innermost_first():
    src = (
        "for ((a=0; a<1; a++)); do\n"
        "  for ((b=0; b<1; b++)); do\n"
        "    for ((c=0; c<1; c++)); do :; done\n"
        "  done\n"
        "done\n"
    )
    fix = arith_for_fixups(src)
    assert list(fix) == [3]
    assert [t for t, _ in fix[3]] == [3, 3, 3, 2, 2, 2, 1, 1, 1]


def test_same_line_nesting_needs_no_fixup():
    src = "for ((a=0;a<1;a++)); do for ((b=0;b<1;b++)); do :; done; done\n"
    assert arith_for_fixups(src) == {}


def test_non_arith_loops_comments_strings_and_heredocs_ignored():
    src = (
        "for ((a=0; a<1; a++)); do\n"  # 1
        "  for x in 1 2; do echo done; done  # for ((fake)); done\n"
        "  echo 'for ((q=0;q<1;q++)); do done'\n"
        '  echo "done for ((z=0;;))"\n'
        "  cat <<EOF\n"
        "for ((h=0; h<1; h++)); do\n"
        "done\n"
        "EOF\n"
        "  while ((a < 5)); do :; done\n"
        "  for ((b=0; b<1; b++)); do :; done\n"  # 10
        "done\n"
    )
    fix = arith_for_fixups(src)
    assert list(fix) == [10]
    assert [t for t, _ in fix[10]] == [10, 10, 10, 1, 1, 1]


def test_function_body_and_empty_exprs():
    src = (
        "f() {\n"
        "  for ((;;)); do\n"  # 2 -> reported at 4
        "    break\n"
        "    for ((k=0; k<1; k++)); do :; done\n"  # 4
        "  done\n"
        "}\n"
    )
    fix = arith_for_fixups(src)
    assert fix[4][3:] == [(2, r"\(\(1\)\)")] * 3


def test_unterminated_loop_is_ignored():
    src = "for ((a=0; a<1; a++)); do\n  for ((b=0; b<1; b++)); do :; done\n"
    assert arith_for_fixups(src) == {}
