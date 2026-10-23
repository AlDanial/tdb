# Crash Detection

When the debuggee raises an unhandled exception, `tdb`:
1. Shows a modal with the full traceback
2. Navigates the Code View to the crash line
3. Populates the Stack View with the exception's call stack
4. Lets you press `R` to restart or `Escape` to dismiss

> Note:  after dismissing the traceback modal, you
> can display it again by hitting `e` when focus is in the Code View.
