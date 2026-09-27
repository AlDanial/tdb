// Demo: drop into tdb at a specific line via tdb.Breakpoint().
//
// Run it directly (not under tdb). Build unoptimized so locals stay
// inspectable, then run the binary from a terminal:
//
//	cd examples/Go/breakpoint_hook_demo
//	go build -gcflags=all=-N -l -o demo . && ./demo
//
// This module points at the checkout's go/tdb through a `replace`
// directive; your own program would just `go get github.com/AlDanial/tdb/go/tdb`.
package main

import (
	"fmt"

	"github.com/AlDanial/tdb/go/tdb"
)

func compute(n int) int {
	total := 0
	localList := []int{1, 2, 3, 4, 5}
	for i := 0; i < n; i++ {
		total += i
	}
	tdb.Breakpoint() // tdb opens here; inspect total and localList
	return total + len(localList)
}

func main() {
	result := compute(10)
	fmt.Println("result =", result)
}
