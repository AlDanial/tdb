import sys
import time

print("-".join(["before", "ran"]), flush=True)
x = 1  # breakpoint line
print("-".join(["after", "ran"]), flush=True)
if len(sys.argv) > 1 and sys.argv[1] == "loop":
    print("-".join(["looping", "now"]), flush=True)
    while True:
        time.sleep(0.2)
line = sys.stdin.readline().strip()
print(f"got:{line}", flush=True)
sys.exit(7)
