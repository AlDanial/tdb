// Build: go build -o pi_digits_go pi_digits.go
// Run:   ./pi_digits_go -r 2 -d 20 -t 2 -p
package main

import (
    "flag"
    "fmt"
    "os"
    "strings"
    "time"
)

type result struct { digits string; elapsed time.Duration }

func piDigits(count int) string {
    a := make([]int64, count*10/3+1)
    for i := range a { a[i] = 2 }
    var out strings.Builder
    previous, pending := byte(0), 0
    for step := 0; step <= count; step++ {
        var q int64
        for i := len(a); i >= 1; i-- {
            x := 10*a[i-1] + q*int64(i)
            a[i-1] = x % int64(2*i-1)
            q = x / int64(2*i-1)
        }
        a[0] = q % 10
        q /= 10
        if q == 9 { pending++ } else if q == 10 {
            out.WriteByte('0'+previous+1)
            out.WriteString(strings.Repeat("0", pending))
            previous, pending = 0, 0
        } else {
            out.WriteByte('0'+previous)
            out.WriteString(strings.Repeat("9", pending))
            previous, pending = byte(q), 0
        }
    }
    out.WriteByte('0'+previous)
    out.WriteString(strings.Repeat("9", pending))
    return out.String()[1:count+1]
}

func main() {
    runs := flag.Int("r", 1, "number of runs")
    count := flag.Int("d", 10, "number of digits")
    threads := flag.Int("t", 1, "workers per run")
    printDigits := flag.Bool("p", false, "print digits")
    flag.IntVar(runs, "n-runs", 1, "number of runs")
    flag.IntVar(count, "n-digits", 10, "number of digits")
    flag.IntVar(threads, "n-threads", 1, "workers per run")
    flag.BoolVar(printDigits, "print", false, "print digits")
    flag.Parse()
    if *runs < 1 || *count < 1 || *threads < 1 { fmt.Fprintln(os.Stderr, "counts must be positive"); os.Exit(2) }
    for run := 1; run <= *runs; run++ {
        runStart := time.Now()
        channels := make([]chan result, *threads)
        for j := range channels {
            channels[j] = make(chan result, 1)
            go func(ch chan result) {
                start := time.Now()
                digits := piDigits(*count)
                ch <- result{digits, time.Since(start)}
            }(channels[j])
        }
        first := ""
        for j, ch := range channels {
            r := <-ch
            if j == 0 { first = r.digits }
            if *threads > 1 { fmt.Printf("Run %3d/%3d, thread %3d/%3d completed in %.6f seconds.\n", run, *runs, j+1, *threads, r.elapsed.Seconds())
            } else { fmt.Printf("Run %3d/%3d completed in %.6f seconds.\n", run, *runs, r.elapsed.Seconds()) }
        }
        if *threads > 1 { fmt.Printf("== Run %3d/%3d completed in %.6f seconds.\n", run, *runs, time.Since(runStart).Seconds()) }
        if run == *runs && *printDigits { fmt.Println(first) }
    }
}
