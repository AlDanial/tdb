package main

import (
	"fmt"
	"sync"
	"time"
)

var mu sync.Mutex

func recvWorker(id int, ch chan int) {
	v := <-ch // parks: nothing ever sends
	fmt.Println(id, v)
}

func lockWorker() {
	mu.Lock() // parks: main holds mu
	defer mu.Unlock()
}

func main() {
	ch := make(chan int)
	mu.Lock()
	for i := 0; i < 3; i++ {
		go recvWorker(i, ch)
	}
	go lockWorker()
	time.Sleep(200 * time.Millisecond) // let workers park
	marker := 42
	fmt.Println("marker =", marker) // BP line 30
	time.Sleep(10 * time.Second)    // window for inspection; test kills earlier
}
