# Devel::TdbRemote -- debugpy-style remote attach for tdb.
#
#   use Devel::TdbRemote;                 # FIRST line of your program
#   ...
#   Devel::TdbRemote::listen(5678);       # non-blocking
#   Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
#
# Pause support: after attaching, tdb opens a SECOND connection to the
# same port (the control channel) and asks the debugger to
# arm_control(). From then on any byte tdb writes there interrupts the
# running program -- see arm_control() for the mechanism.
#
# Also works via `perl -d:TdbRemote prog.pl` or PERL5OPT=-d:TdbRemote.
# Only code compiled AFTER the debugger is armed can be stepped or
# breakpointed -- that is why the `use` line must come first.
package Devel::TdbRemote;

use strict;
use warnings;
use IO::Socket::INET ();
use File::Basename   ();
use File::Spec       ();
use Cwd              ();

our $VERSION = '1.1';
my $LISTENER;
my $CONTROL;

BEGIN {
    # Arm the debugger unless perl already did (-d / -d:TdbRemote).
    # NonStop: perl5db initializes without a TTY and lets the program
    # run freely (past its own compile-time stop) until we flip
    # $DB::single in wait_for_client(). This mirrors perl5db's own
    # RemotePort mode, which also relies on NonStop-style deferral
    # while it waits to hook up a socket.
    $ENV{PERLDB_OPTS} = 'NonStop=1'
      unless defined $ENV{PERLDB_OPTS} && length $ENV{PERLDB_OPTS};
    $^P = 0x73f unless $^P & 0x02;
    unless ( defined &DB::DB ) {
        package DB;
        require 'perl5db.pl';
    }
}

sub listen {
    my ( $port, $host ) = @_;
    $host = '0.0.0.0' unless defined $host;
    $LISTENER = IO::Socket::INET->new(
        LocalAddr => $host,
        LocalPort => $port,
        Listen    => 1,
        ReuseAddr => 1,
    ) or die "Devel::TdbRemote: cannot listen on $host:$port: $!\n";
    return;
}

sub wait_for_client {
    die "Devel::TdbRemote: call listen(\$port) first\n" unless $LISTENER;
    my $client = $LISTENER->accept
      or die "Devel::TdbRemote: accept failed: $!\n";
    $client->autoflush(1);

    # Install the socket as perl5db's terminal. perl5db's own
    # RemotePort/connect_remoteport() path does exactly this: store the
    # IO::Socket object directly in the package scalars $IN/$OUT (which
    # ARE $DB::IN/$DB::OUT -- perl5db declares them with `our`, not
    # `my`). No typeglob dup is involved on that path, and helpers.pl's
    # _out() already prefers $DB::OUT when it holds a ref, so plain
    # scalar assignment satisfies both perl5db and our helpers with one
    # write. $DB::LINEINFO is deliberately left untouched here: setterm()
    # (invoked the moment $single next trips DB::DB) does
    # `$LINEINFO = $OUT unless defined $LINEINFO`, which picks up this
    # same socket automatically -- pre-assigning it ourselves would just
    # duplicate that.
    { no warnings 'once'; $DB::IN = $DB::OUT = $client; }

    # Load the data-extraction helpers that live next to this module.
    # __FILE__ can be a relative path (e.g. `-I../perl`) as found via
    # @INC; resolve to an absolute path before handing it to `do`, since
    # `do` on a relative, directory-bearing filename still consults
    # @INC, and '.' has not been in the default @INC since perl 5.26 --
    # a relative $helpers would fail to load from a cwd other than the
    # one -I was resolved against.
    my $dir     = File::Basename::dirname( Cwd::abs_path(__FILE__) );
    my $helpers = File::Spec->catfile( $dir, File::Spec->updir, 'helpers.pl' );
    do $helpers or die "TdbRemote: cannot load $helpers: " . ( $@ || $! ) . "\n";

    # Let the program actually exit when it falls off the end after a
    # final `c`, instead of perl5db's default of parking at a "Debugged
    # program terminated" prompt (inhibit_exit defaults to true under
    # plain -d too -- this isn't NonStop-specific).
    { no warnings 'once'; $DB::inhibit_exit = 0; $DB::signal = 0; }

    # Stop at the statement after this call, debugpy-style.
    $DB::single = 1;
    return;
}

# Accept tdb's control connection and arm asynchronous pause.
#
# Invoked by the adapter as a debugger command right after the attach
# handshake, so the process is stopped at a perl5db prompt and the
# adapter has already connected a second time to the listener (that
# connection sits in the kernel backlog until accepted here). Replies
# with one TDB>>>{json}<<<TDB line: {"control":1} when armed, or
# {"control":0,"error":...} when it could not be -- the adapter then
# keeps pause gated exactly as before. Never dies: a failure here must
# not take down the attach.
#
# Mechanism: the control socket is put in O_ASYNC mode with this
# process as its owner, so the kernel delivers SIGIO the moment tdb
# writes to it. The handler sets $DB::signal -- the very flag perl5db's
# own SIGINT handler (DB::catch) sets -- so DB::DB takes control at the
# next statement and clears it, giving attach-mode pause the same
# semantics as launch mode's SIGINT. Nothing touches the debug socket,
# so no stray bytes ever reach perl5db's command stream. Compared to a
# $SIG{ALRM} poll this costs nothing while idle, reacts immediately,
# and leaves alarm()/sleep() to the program. Platforms without
# O_ASYNC (Windows) simply report control => 0.
sub arm_control {
    my $reply = eval {
        die "listen() was never called\n" unless $LISTENER;
        require Fcntl;
        require IO::Select;
        if ($CONTROL) {    # re-attach after a detach: drop the stale one
            close $CONTROL;
            undef $CONTROL;
        }
        # Never block the debuggee on a client that did not connect.
        IO::Select->new($LISTENER)->can_read(5)
          or die "no pending control connection\n";
        my $c = $LISTENER->accept or die "accept failed: $!\n";
        $c->blocking(0);
        my $flags = fcntl( $c, Fcntl::F_GETFL(), 0 );
        defined $flags or die "F_GETFL: $!\n";
        fcntl( $c, Fcntl::F_SETOWN(), $$ ) or die "F_SETOWN: $!\n";
        fcntl( $c, Fcntl::F_SETFL(), $flags | Fcntl::O_ASYNC() )
          or die "F_SETFL O_ASYNC: $!\n";
        $SIG{IO} = \&DB::_tdb_on_control_io;
        $CONTROL = $c;
        { control => 1 };
    };
    unless ($reply) {
        my $err = $@;
        $err =~ s/\s+\z//;
        $reply = { control => 0, error => $err };
    }
    Devel::TdbHelper::_emit($reply);
    return;
}

# Compiled in package DB on purpose: perl never emits a debugger
# breakpoint op for statements in the debugger's own package, so the
# handler runs invisibly and the stop it requests lands on the NEXT
# statement of the interrupted program -- the user's code. Defined in
# this file's usual package, the first statement after `$DB::signal = 1`
# would itself trip DB::DB and the pause would surface inside this
# handler, in TdbRemote.pm, with `package Devel::TdbRemote` as the
# evaluate scope.
{
    package DB;

    sub _tdb_on_control_io {
        return unless $CONTROL;
        my $buf = '';
        my $n = sysread( $CONTROL, $buf, 4096 );
        return unless defined $n;    # EAGAIN or the like: nothing to do
        if ( $n == 0 ) {
            # tdb detached: disarm without disturbing the program.
            close $CONTROL;
            undef $CONTROL;
            $SIG{IO} = 'DEFAULT';
            return;
        }
        $DB::signal = 1;
        return;
    }
}

1;
