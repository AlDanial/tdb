# Layout

```
┌─ Header ──────────────────────────────────────────────┐
├─ Menu Bar (File / Configure / Help)───────────────────┤
│                           │                           │
│   Code View               │  Console View (stdio)     │
│   (source + breakpoints)  ├───────────────────────────┤
│                           │  Variable View (tree)     │
│                           ├───────────────────────────┤
│                           │  Stack View (call stack)  │
├─ Status Bar ──────────────────────────────────────────┤
│                           │                           │
│  Evaluate Console (REPL)  │  Breakpoint View (table)  │
│                           │                           │
├─ Footer (keybindings) ────────────────────────────────┤
└───────────────────────────────────────────────────────┘
```

The status bar shows the current execution state (running, paused,
breakpoint hit) and location.
The footer shows the most relevant keybindings for the current mode.
