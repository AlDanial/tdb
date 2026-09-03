package main

import "fmt"

func add(a int, b int) int {
	result := a + b
	return result // BP line 7
}

func main() {
	x := 5
	y := add(x, 7)
	fmt.Println("total =", y)
}
