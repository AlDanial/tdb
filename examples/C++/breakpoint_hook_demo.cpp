// Demo: drop into tdb at a specific line via tdb_breakpoint().
//
// Run it directly (not under tdb). Build unoptimized so locals stay
// inspectable, then run the binary from a terminal:
//
//   g++ -g -O0 -I "$(tdb --info | awk -F': ' '/tdb.h dir/ {print $2}')" \
//       -o demo breakpoint_hook_demo.cpp && ./demo
#include <iostream>
#include <vector>
#include "tdb.h"

static int compute(int n) {
    int total = 0;
    std::vector<int> local_list{1, 2, 3, 4, 5};
    for (int i = 0; i < n; i++) {
        total += i;
    }
    tdb_breakpoint(); // tdb opens here; inspect total and local_list
    return total + static_cast<int>(local_list.size());
}

int main() {
    int result = compute(10);
    std::cout << "result = " << result << "\n";
    return 0;
}
