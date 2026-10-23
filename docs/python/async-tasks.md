# Async Task Inspector

For programs using `asyncio`, the menu bar shows an **Async Tasks (N)** label with the count of
active tasks (updated each time the program stops). Click it to open a full-screen modal:

- **Left pane**: list of all tasks with name, state (pending/done/cancelled), awaiting
  primitive (`Lock.acquire`, `Queue.get`, `asyncio.sleep`, …), and coroutine
- **Right pane**: detail view with full stack trace and an expandable variable tree (same
as the main Variables View) for the selected task
- Press `g` to switch the right pane to the **wait graph** which is a tree showing
  each blocked
  task, the asyncio primitive it's parked on, and the task(s) holding that primitive.
  Cycles (deadlocks) are highlighted in red both in the task table and as a "Deadlock
  cycles" section at the top of the graph. Selecting a node in the tree highlights the
  corresponding task in the table.
- Navigate with arrow keys; press `r` to refresh, `Escape` to close

> Note:  Async task relationships may be directed acyclic graphs (DAGs) rather than trees
> but I don't know of a way to visualize DAGs in textual.

RPC equivalents:

```bash
# List all tasks
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tasks","params":[]}'

# Inspect a specific task by name
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_task","params":["Task-1"]}'

# Show wait graph and any deadlock cycles
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"wait_graph","params":[]}'
```
