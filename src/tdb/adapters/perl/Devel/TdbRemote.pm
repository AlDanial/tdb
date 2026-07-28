# Devel::TdbRemote -- debugpy-style remote attach for tdb.
#
#   use Devel::TdbRemote;                 # FIRST line of your program
#   ...
#   Devel::TdbRemote::listen(5678);       # non-blocking
#   Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
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

our $VERSION = '1.0';
my $LISTENER;

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

1;
