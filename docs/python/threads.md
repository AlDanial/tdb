# Thread Inspector

The menu bar shows a **Threads (N)** label when the program has 2 or more threads. Click it to open a modal with:

- **Left pane**: list of threads with ID and name (current thread shown in bold)
- **Right pane**: full stack trace and expandable variable tree for the selected thread's top frame
- Navigate with arrow keys; press `r` to refresh, `Escape` to close

RPC equivalents:

```bash
# List all threads (* marks current)
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_threads","params":[]}'

# Inspect a specific thread by ID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_thread","params":[1]}'
```
