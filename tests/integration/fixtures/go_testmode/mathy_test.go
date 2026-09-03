package mathy

import "testing"

func TestDouble(t *testing.T) {
	got := Double(21) // BP line 6
	if got != 42 {
		t.Fatalf("got %d", got)
	}
}
