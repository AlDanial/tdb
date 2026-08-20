# Force the debuggee's stdout to be unbuffered.
#
# Ruby defaults STDOUT.sync to false, so when the debuggee runs with its
# stdout attached to a pipe (as it does under the tdb bridge, which reads
# rdbg's stdout/stderr pipes to relay program output as DAP `output`
# events), `puts` output is fully buffered and only flushed at process
# exit.  That makes program output invisible during the run.  The bridge
# injects this file via RUBYOPT (`-r`) so `puts` output streams live.
STDOUT.sync = true