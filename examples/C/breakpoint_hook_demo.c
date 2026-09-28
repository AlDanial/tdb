/* Demo: drop into tdb at a specific line via tdb_breakpoint().
 *
 * Run it directly (not under tdb). Build unoptimized so locals stay
 * inspectable, then run the binary from a terminal:
 *
 *   gcc -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
 *       -o demo breakpoint_hook_demo.c && ./demo
 */
#include <stdio.h>
#include "tdb.h"

static int compute(int n) {
    int total = 0;
    int local_list[5] = {1, 2, 3, 4, 5};
    for (int i = 0; i < n; i++) {
        total += i;
    }
    tdb_breakpoint(); /* tdb opens here; inspect total and local_list */
    return total + local_list[4];
}

int main(void) {
    int result = compute(10);
    printf("result = %d\n", result);
    return 0;
}
