import os
import sys
import time

print("-".join(["before", "ran"]), flush=True)
x = 1  # breakpoint line
print("-".join(["after", "ran"]), flush=True)
if len(sys.argv) > 1 and sys.argv[1] == "loop":
    print("-".join(["looping", "now"]), flush=True)
    while True:
        time.sleep(0.2)
if len(sys.argv) > 1 and sys.argv[1] == "soon":
    print("-".join(["looping", "now"]), flush=True)
    time.sleep(1.5)
    os._exit(7)  # sys.exit(7) would stop on the SystemExit exception
line = sys.stdin.readline().strip()
print(f"got:{line}", flush=True)
sys.exit(7)
