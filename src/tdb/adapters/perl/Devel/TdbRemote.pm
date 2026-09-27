# Devel::TdbRemote -- debugpy-style remote attach for tdb.
#
#   use Devel::TdbRemote;                 # FIRST line of your program
#   ...
#   Devel::TdbRemote::listen(5678);       # non-blocking
#   Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
#
# Or, tdb.breakpoint()-style, let the program open tdb on itself:
#
#   use Devel::TdbRemote;                 # still the FIRST line
#   ...
#   Devel::TdbRemote::breakpoint();       # tdb opens, paused on the next line
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
use IO::Select       ();
use Socket           ();
use File::Basename   ();
use File::Spec       ();
use Cwd              ();
use POSIX            ();

our $VERSION = '1.2';
my $LISTENER;
my $CONTROL;
my $CLIENT;        # the accepted debug socket (also $DB::IN / $DB::OUT)
my $DETACHED = 0;  # tdb announced it is leaving (see detach())
my $TDB_PID;       # the tdb subprocess breakpoint() spawned, if any
my $PREV_SIGIO;    # the program's own $SIG{IO}, restored when tdb detaches

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
    _adopt_client($client);

    # Stop at the statement after this call, debugpy-style. This must be
    # the sub's LAST statement: a trailing `return;` would be a statement
    # of its own and the stop would land on it, inside this file.
    $DB::single = 1;
}

# Install an accepted socket as perl5db's terminal and load the helpers.
sub _adopt_client {
    my ($client) = @_;
    $client->autoflush(1);

    # Install the socket as perl5db's terminal. perl5db's own
    # RemotePort/connect_remoteport() path does exactly this: store the
    # IO::Socket object directly in the package scalars $IN/$OUT (which
    # ARE $DB::IN/$DB::OUT -- perl5db declares them with `our`, not
    # `my`). No typeglob dup is involved on that path, and helpers.pl's
    # _out() already prefers $DB::OUT when it holds a ref, so plain
    # scalar assignment satisfies both perl5db and our helpers with one
    # write. $DB::LINEINFO gets the `main::(file:N):` + source-line echo
    # perl5db prints at every stop; setterm() only defaults it
    # (`$LINEINFO = $OUT unless defined $LINEINFO`), and by the time we
    # run it already holds perl5db's console -- the tty, i.e. the very
    # screen tdb is drawing on in the breakpoint() case -- so it must be
    # re-pointed at the socket explicitly. The adapter discards that
    # chatter. Re-attaching after a detach re-points all three again.
    { no warnings 'once'; $DB::IN = $DB::OUT = $DB::LINEINFO = $client; }
    $CLIENT   = $client;
    $DETACHED = 0;

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
    return;
}

# Drop into tdb at the call site (tdb.breakpoint() analog).
#
# First call: listen on an ephemeral loopback port (unless the program
# already called listen()), spawn `tdb --lang perl -r 127.0.0.1:PORT`
# on this tty, wait for it to connect, then stop at the next statement.
# Later calls reuse the running tdb: it simply receives another stop.
# If tdb was quit in between, a fresh one is spawned and attached.
#
# No-op when stdin/stdout are not ttys, so it is safe to leave in code
# that sometimes runs headless. Never dies: if tdb cannot be started or
# exits before attaching, warn and let the program run on.
#
# `tdb` is found via $ENV{TDB}, else on PATH.
sub breakpoint {
    return unless -t STDIN && -t STDOUT;
    unless ( _client_connected() ) {
        # The old tdb is gone or going: it has already closed the debug
        # socket, but its TUI may still be tearing down the terminal. Let
        # it finish (bounded) before a new one takes the screen over.
        _wait_tdb_exit(3);
        eval {
            Devel::TdbRemote::listen( 0, '127.0.0.1' ) unless $LISTENER;
            1;
        } or do { warn $@; return; };
        $TDB_PID = _spawn_tdb( $LISTENER->sockport );
        return unless defined $TDB_PID;
        my $client = _accept_from($TDB_PID);
        return unless $client;
        _adopt_client($client);
    }

    # LAST statement on purpose -- see wait_for_client().
    $DB::single = 1;
}

# Is a debugger client still attached? Judged by detach()'s flag and the
# debug SOCKET, never by whether the tdb process is alive: after `q`, tdb
# disconnects first and exits some time later, and a breakpoint() reached
# in that window must see "nobody attached" -- arming a stop with the
# peer gone makes perl5db write its prompt to a dead socket and the
# program dies of SIGPIPE. The socket check covers a tdb that died
# without saying goodbye.
sub _client_connected {
    return 0 if $DETACHED;
    return 0 unless $CLIENT && defined fileno($CLIENT);
    return 1 unless IO::Select->new($CLIENT)->can_read(0);
    # Readable while we are running means EOF (perl5db drained every
    # command before resuming us), unless a byte really is pending.
    my $buf = '';
    my $peeked = recv( $CLIENT, $buf, 1, Socket::MSG_PEEK() );
    return 0 unless defined $peeked;    # error: treat as gone
    return length($buf) ? 1 : 0;
}

sub _wait_tdb_exit {
    my ($seconds) = @_;
    my $deadline = time + $seconds;
    while ( _tdb_alive() && time < $deadline ) {
        select( undef, undef, undef, 0.05 );
    }
    return;
}

sub _tdb_alive {
    return 0 unless defined $TDB_PID;
    my $reaped = waitpid( $TDB_PID, POSIX::WNOHANG() );
    return 1 if $reaped == 0;         # still running
    return 0 if $reaped == $TDB_PID;  # exited, now reaped
    # -1: not our child to reap (e.g. the program set $SIG{CHLD} to
    # 'IGNORE'); fall back to a liveness probe.
    return kill( 0, $TDB_PID ) ? 1 : 0;
}

sub _spawn_tdb {
    my ($port) = @_;
    my @cmd = ( ( $ENV{TDB} || 'tdb' ), '--lang', 'perl', '-r', "127.0.0.1:$port" );
    if ( $^O eq 'MSWin32' ) {
        my $pid = system( 1, @cmd );    # asynchronous spawn, returns pid
        return $pid if $pid > 0;
        warn "Devel::TdbRemote: cannot start $cmd[0]: $!\n";
        return undef;
    }
    my $pid = fork;
    unless ( defined $pid ) {
        warn "Devel::TdbRemote: cannot fork to start tdb: $!\n";
        return undef;
    }
    if ( $pid == 0 ) {
        # Child: become tdb. On failure skip END blocks and destructors
        # (they belong to the parent program) and exit hard.
        exec @cmd
          or do {
            print STDERR "Devel::TdbRemote: cannot start $cmd[0]: $!\n";
            POSIX::_exit(127);
          };
    }
    return $pid;
}

# accept() on $LISTENER, giving up if the tdb child exits first (tdb not
# installed, bad port, tdb crashed at startup) so a failed spawn never
# leaves the program hanging in accept.
sub _accept_from {
    my ($pid) = @_;
    my $sel = IO::Select->new($LISTENER);
    while (1) {
        if ( $sel->can_read(0.25) ) {
            my $client = $LISTENER->accept;
            return $client if $client;
            warn "Devel::TdbRemote: accept failed: $!\n";
            return undef;
        }
        unless ( _tdb_alive() ) {
            warn "Devel::TdbRemote: tdb exited before attaching; continuing\n";
            return undef;
        }
    }
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
        $PREV_SIGIO = $SIG{IO} unless $CONTROL;
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

# tdb is about to leave: issued by the adapter from a debugger prompt,
# right before it clears breakpoints, resumes us and closes its sockets.
#
# Two jobs. (1) Undo arm_control() while tdb's end of the control socket
# is still open: once OUR fd is closed the kernel has nobody to signal,
# so the detach raises no SIGIO at all (the async EOF branch in
# _tdb_on_control_io stays as the fallback for an abrupt tdb death).
# (2) Record that nobody is attached any more. breakpoint() must not
# learn that from the debug socket alone: the adapter's `c` reaches us
# before its close() does, and a breakpoint() on the very next statement
# would still see a live-looking socket, arm a stop, and have perl5db
# write its prompt to a peer that is gone -- SIGPIPE, program dead.
# Replies {"control":0,"detached":1} so the adapter can await it.
sub detach {
    $DETACHED = 1;
    if ($CONTROL) {
        close $CONTROL;
        undef $CONTROL;
        $SIG{IO} = _restored_sigio();
    }
    Devel::TdbHelper::_emit( { control => 0, detached => 1 } );
    return;
}

# The program's own SIGIO disposition, or 'IGNORE'. Never 'DEFAULT':
# SIGIO's default action kills the process, and a stray one can still
# arrive after we have let go of the socket.
sub _restored_sigio {
    return ( defined $PREV_SIGIO && $PREV_SIGIO ne 'DEFAULT' && $PREV_SIGIO ne '' )
      ? $PREV_SIGIO
      : 'IGNORE';
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
            # tdb detached: disarm without disturbing the program. Never
            # fall back to 'DEFAULT' -- SIGIO's default action is to kill
            # the process, and the kernel can deliver a second SIGIO for
            # the same peer close (readable + hangup) after this handler
            # has already run. Hand back the program's own handler if it
            # had one, otherwise ignore.
            close $CONTROL;
            undef $CONTROL;
            $SIG{IO} = Devel::TdbRemote::_restored_sigio();
            return;
        }
        $DB::signal = 1;
        return;
    }
}

1;
