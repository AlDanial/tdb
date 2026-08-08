# Devel::TdbCompile -- arms perl5db to trap during the COMPILE phase so
# BEGIN blocks (and any other compile-time statement of the debuggee's
# own file) become steppable, instead of perl5db's default of only
# ever stopping once runtime execution begins (see perldebug,
# "Debugging Compile-Time Statements", and the plan's Background 1-3).
#
#   perl -d -I<dir of this file> -MDevel::TdbCompile prog.pl
#
# with $ENV{TDB_COMPILE_FILE} set (by session.py::launch) to the exact
# string perl will report as the program's file in `caller` -- i.e.
# argv's program path, verbatim (perl does not resolve/canonicalize
# it; confirmed empirically against 5.40.1).
#
# Only launch mode loads this module. Attach mode (Devel::TdbRemote)
# is unaffected: the debuggee is already running by the time tdb
# attaches, so there is no compile phase left to catch.
package Devel::TdbCompile;

BEGIN {
    my $target = $ENV{TDB_COMPILE_FILE};
    return unless defined $target && length $target;

    # perl5db.pl must already be loaded (`-d` guarantees this runs
    # before us) so &DB::DB exists to wrap.
    my $orig = \&DB::DB;
    no warnings 'redefine';

    # MUST self-uninstall (`*DB::DB = $orig`) the moment
    # ${^GLOBAL_PHASE} leaves 'START', restoring the original DB::DB
    # before the tail-call `goto`. This is not a micro-optimisation --
    # it is required for correctness. An earlier version left the
    # wrapper installed for the rest of the process's life and made
    # the RUN-phase branch a bare `goto &$orig` pass-through; verified
    # empirically, that combination reliably makes perl die with
    # "Out of memory in perl:util:safesysmalloc" once run-phase
    # execution proceeds. Self-uninstalling avoids that entirely: only
    # the compile phase (a handful of statements) ever pays the cost
    # of this wrapper at all.
    #
    # Self-uninstalling does leak exactly one stop at this
    # reassignment/goto line, INSIDE this file, during the START->RUN
    # transition -- a shim implementation detail that would otherwise
    # surface in the user-visible debugging session. That is handled
    # on the Perl side by `_user_frames` in helpers.pl, which already
    # skips frames whose package is Devel::TdbCompile, and belt-and-
    # suspenders on the adapter side by auto-stepping past any stop
    # that still lands in TdbCompile.pm (see server.py).
    *DB::DB = sub {
        if ( ${^GLOBAL_PHASE} ne 'START' ) {
            *DB::DB = $orig;
            goto &$orig;
        }
        # Still compiling: only trap statements belonging to the
        # debuggee's own file (BEGIN blocks, `use` pragmas, etc.) --
        # naively arming $DB::single here would also drag the user
        # through strict.pm/warnings.pm/every `use`d module's own
        # compile-time internals.
        my ( undef, $file ) = caller;
        return unless defined $file && $file eq $target;
        goto &$orig;
    };

    # Arm single-stepping before perl even starts compiling the
    # target file, so the very first compile-time statement traps.
    $DB::single = 1;
}

1;
