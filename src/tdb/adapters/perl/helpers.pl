# Devel::TdbHelper -- data-extraction helpers injected into the
# debuggee by tdb's Perl DAP adapter. Every public sub prints exactly
# one TDB>>>{json}<<<TDB line and returns nothing. All entry points
# trap their own errors: a helper bug must degrade to a JSON error
# reply, never a debuggee crash.
package Devel::TdbHelper;

use strict;
use warnings;
use JSON::PP ();
use Scalar::Util qw(blessed reftype);

our $PROTOCOL = 1;
my $JSON = JSON::PP->new->canonical->allow_unknown;

# The debuggee's own STDOUT is block-buffered when it's a pipe (the
# adapter's case). perl5db intercepts normal program exit and parks at
# a prompt instead of truly _exit()-ing, so buffered output would sit
# unflushed -- invisible to the adapter -- until a real process exit.
# Force line-buffering-equivalent (full autoflush) on STDOUT so stdout
# forwarding is live, without touching whatever filehandle happens to
# be selected right now.
{
    my $prev = select(STDOUT);
    $| = 1;
    select($prev);
}

# Expandable-ref stash: id -> ref. Cleared at each stop (location()).
our %REG;
our $NEXT_ID = 1;

sub _out {
    my ($line) = @_;
    # Under real perl5db, the RemotePort/console filehandle lives in the
    # package scalar $DB::OUT (an IO::Socket/typeglob-ref), NOT the
    # typeglob *DB::OUT -- that glob is never populated for RemotePort
    # sessions. Writing here (rather than STDOUT) keeps our JSON marker
    # interleaved with the prompt stream the adapter's parser reads.
    my $fh = ( defined $DB::OUT && ref($DB::OUT) ) ? $DB::OUT : \*STDOUT;
    print {$fh} $line;
    return;
}

sub _emit {
    my ($data) = @_;
    my $enc = eval { $JSON->encode($data) };
    $enc = '{"error":"json encode failed"}' unless defined $enc;
    _out("TDB>>>$enc<<<TDB\n");
    return;
}

sub _emit_error {
    my ($msg) = @_;
    $msg =~ s/\s+\z//;
    _emit({ error => "$msg" });
    return;
}

# Walk caller() skipping adapter/debugger frames. Returns a list of
# [file, line, subname] innermost-first. The frame a payload describes
# is the debuggee's, never ours.
sub _user_frames {
    my @frames;
    my $i = 0;
    while ( my @c = caller($i) ) {
        my ( $pkg, $file, $line ) = @c[ 0, 1, 2 ];
        $i++;
        next if $pkg =~ /\A(?:DB\b|Devel::TdbHelper)/;
        next if $file =~ /\(eval \d+\)/;
        my $sub = ( caller($i) )[3];    # sub that contains this frame
        push @frames, [ $file, $line, $sub ];
    }
    return @frames;
}

sub location {
    eval {
        %REG     = ();
        $NEXT_ID = 1;
        my @frames = _user_frames();
        my $top = $frames[0] || [ '?', 0, undef ];
        _emit(
            {
                version => $PROTOCOL,
                file    => $top->[0],
                line    => $top->[1] + 0,
                sub     => $top->[2],
            }
        );
        1;
    } or _emit_error($@);
    return;
}

sub stack {
    eval {
        my @out;
        for my $f (_user_frames()) {
            push @out, { file => $f->[0], line => $f->[1] + 0, sub => $f->[2] };
        }
        _emit( { frames => \@out } );
        1;
    } or _emit_error($@);
    return;
}

sub breakable {
    my ($file) = @_;
    eval {
        no strict 'refs';
        # Merely dereferencing \@{"main::_<$file"} for a file perl has
        # NOT yet compiled autovivifies that array, which silently
        # poisons perl5db's own breakpoint machinery for that filename
        # for the rest of the process (b <file>:<line> is later accepted
        # but never fires). `exists $main::{"_<$file"}` checks whether
        # perl's compiler has ever created the "_<$file" typeglob for
        # this file WITHOUT dereferencing/autovivifying the array inside
        # it, so it's safe to call speculatively on files that may not
        # be loaded yet. Confirmed via A/B probe (Task 10 report).
        unless ( exists $main::{"_<$file"} ) {
            _emit( { lines => [], unloaded => 1 } );
            return 1;
        }
        my $src = \@{"main::_<$file"};
        my @lines;
        for my $n ( 1 .. $#{$src} ) {
            no warnings 'numeric', 'uninitialized';
            push @lines, $n if defined $src->[$n] && $src->[$n] != 0;
        }
        _emit( { lines => \@lines } );
        1;
    } or _emit_error($@);
    return;
}

sub source {
    my ($file) = @_;
    eval {
        no strict 'refs';
        my $src = \@{"main::_<$file"};
        my $text = join( '', grep { defined } @{$src}[ 1 .. $#{$src} ] );
        _emit( { text => $text } );
        1;
    } or _emit_error($@);
    return;
}

use overload ();

my $HAVE_PADWALKER = eval { require PadWalker; 1 } ? 1 : 0;

sub _stash {
    my ($ref) = @_;
    my $id = $NEXT_ID++;
    $REG{$id} = $ref;
    return $id;
}

# Returns (display_string, expand_id). expand_id 0 => atom.
sub _preview {
    my ($v) = @_;
    return ( 'undef', 0 ) unless defined $v;
    my $rt = reftype($v);
    if ( !defined $rt ) {
        # plain scalar
        return ( "$v", 0 ) if $v =~ /\A-?\d+(?:\.\d+)?\z/;
        my $s = "$v";
        $s = substr( $s, 0, 120 ) . '...' if length($s) > 120;
        $s =~ s/'/\\'/g;
        return ( "'$s'", 0 );
    }
    my $class = blessed($v);
    my $tied =
        $rt eq 'HASH'   ? tied %$v
      : $rt eq 'ARRAY'  ? tied @$v
      : $rt eq 'SCALAR' ? tied $$v
      :                   undef;
    my $base =
        $rt eq 'HASH'  ? sprintf( 'HASH(%d keys)',  scalar keys %$v )
      : $rt eq 'ARRAY' ? sprintf( 'ARRAY(%d)',      scalar @$v )
      : $rt eq 'CODE'  ? overload::StrVal($v)
      :                  overload::StrVal($v);
    $base = $class . '=' . overload::StrVal($v) if defined $class;
    $base .= ' (tied via ' . ref($tied) . ')' if $tied;
    my $expandable = ( $rt eq 'HASH' || $rt eq 'ARRAY' || $rt eq 'SCALAR' ) ? 1 : 0;
    $expandable = 0 if $rt eq 'SCALAR' && !ref($$v) && !defined blessed($v);
    return ( $base, $expandable ? _stash($v) : 0 );
}

sub _test_preview {
    my ($v) = @_;
    my ( $value, $id ) = _preview($v);
    _emit( { value => $value, id => $id } );
    return;
}

sub _entry {
    my ( $name, $v ) = @_;
    my ( $value, $id ) = _preview($v);
    return { name => "$name", value => $value, id => $id };
}

sub expand {
    my ($id) = @_;
    eval {
        my $ref = $REG{$id};
        if ( !defined $ref ) { _emit( { error => "stale ref $id" } ); return 1; }
        my $rt = reftype($ref);
        my @out;
        if ( $rt eq 'HASH' ) {
            push @out, _entry( $_, $ref->{$_} ) for sort keys %$ref;
        }
        elsif ( $rt eq 'ARRAY' ) {
            push @out, _entry( "[$_]", $ref->[$_] ) for 0 .. $#{$ref};
        }
        elsif ( $rt eq 'SCALAR' || $rt eq 'REF' ) {
            push @out, _entry( 'deref', $$ref );
        }
        _emit( { vars => \@out } );
        1;
    } or _emit_error($@);
    return;
}

# --- lexicals -------------------------------------------------------
# Order: PadWalker (if installed) -> core-B read-only pad walk for
# named subs -> degraded marker. Frame numbering matches _user_frames.

sub _lexicals_for_frame {
    my ($frame) = @_;
    if ($HAVE_PADWALKER) {
        # peek_my's LEVEL counts real sub-call frames only: an eval
        # BLOCK shows up in caller() (subname "(eval)") but is
        # transparent to peek_my, so it must not consume a level.
        # Walk out through our own frames the same way _user_frames
        # skips them, tracking the peek_my level in parallel.
        my $level = 0;
        my $i     = 0;
        while ( my @c = caller($i) ) {
            $i++;
            $level++ unless defined $c[3] && $c[3] eq '(eval)';
            next if $c[0] =~ /\A(?:DB\b|Devel::TdbHelper)/;
            next if $c[1] =~ /\(eval \d+\)/;
            last if $frame-- == 0;
        }
        my $pad = eval { PadWalker::peek_my($level) };
        return ( undef, "PadWalker peek failed: $@" ) unless $pad;
        return ( $pad, undef );
    }
    # Core-B fallback: resolve the frame's containing sub by name and
    # read its pad. Anonymous subs and evals can't be resolved by name.
    my @frames  = _user_frames();
    my $subname = $frames[$frame] && $frames[$frame][2];
    return ( undef, 'lexicals unavailable -- install PadWalker' )
      if !$subname || $subname =~ /__ANON__/;
    my ( $pad, $err ) = eval {
        require B;
        no strict 'refs';
        my $cv = \&{$subname};
        my $b  = B::svref_2object($cv);
        return ( undef, 'no pad' ) unless $b->isa('B::CV');
        my $padlist = $b->PADLIST;
        my @names   = $padlist->ARRAYelt(0)->ARRAY;
        my $depth   = $b->DEPTH || 1;
        my @vals    = $padlist->ARRAYelt($depth)->ARRAY;
        my %pad;
        for my $i ( 0 .. $#names ) {
            my $n = $names[$i];
            next unless ref($n) && $n->can('PV') && !$n->isa('B::SPECIAL');
            my $name = eval { $n->PV } or next;
            next unless $name =~ /\A[\$\@\%]\w/;
            my $sv = $vals[$i] or next;
            $pad{$name} = eval { $sv->object_2svref };
        }
        ( \%pad, undef );
    };
    return ( undef, 'lexicals unavailable -- install PadWalker' )
      if !$pad || $@;
    return ( $pad, undef );
}

sub scopes {
    my ($frame) = @_;
    eval {
        _emit(
            {
                scopes => [
                    { name => 'Lexicals', kind => 'lexicals' },
                    { name => 'Globals',  kind => 'globals' },
                    { name => 'Specials', kind => 'specials' },
                ]
            }
        );
        1;
    } or _emit_error($@);
    return;
}

sub vars {
    my ( $frame, $kind ) = @_;
    eval {
        my @out;
        if ( $kind eq 'lexicals' ) {
            my ( $pad, $degraded ) = _lexicals_for_frame($frame);
            if ($degraded) { _emit( { vars => [], degraded => $degraded } ); return 1; }
            for my $name ( sort keys %$pad ) {
                my $ref = $pad->{$name};
                my $val =
                    $name =~ /\A\$/ ? $$ref
                  : $name =~ /\A\@/ ? $ref
                  :                   $ref;
                push @out, _entry( $name, $val );
            }
        }
        elsif ( $kind eq 'globals' ) {
            my @frames = _user_frames();
            my $file = $frames[$frame] ? $frames[$frame][0] : '';
            no strict 'refs';
            my $pkg = 'main';
            for my $name ( sort keys %{"${pkg}::"} ) {
                next if $name =~ /::\z/ || $name =~ /\A(?:_<|[^a-zA-Z])/;
                my $full = "${pkg}::$name";
                push @out, _entry( "\$$name", ${$full} ) if defined ${$full};
            }
        }
        elsif ( $kind eq 'specials' ) {
            push @out, _entry( '@_', [ @DB::args ] ) if @DB::args;
            push @out, _entry( '$_',    $_ );
            push @out, _entry( '$@',    $@ );
            push @out, _entry( '$!',    "$!" );
            push @out, _entry( '$0',    $0 );
            push @out, _entry( '@ARGV', \@ARGV );
            push @out, _entry( '@INC',  \@INC );
            push @out, _entry( '%ENV',  \%ENV );
            push @out, _entry( '$/',    $/ );
            push @out, _entry( '$\\',   $\ );
        }
        _emit( { vars => \@out } );
        1;
    } or _emit_error($@);
    return;
}

sub emit_eval {
    my ( $results, $err ) = @_;
    eval {
        if ($err) {
            my $msg = "$err";
            $msg =~ s/\s+\z//;
            _emit( { error => $msg } );
            return 1;
        }
        if ( @$results == 1 ) {
            my ( $value, $id ) = _preview( $results->[0] );
            _emit( { value => $value, id => $id } );
        }
        elsif ( @$results == 0 ) {
            _emit( { value => '()', id => 0 } );
        }
        else {
            my @parts = map { ( _preview($_) )[0] } @$results;
            my ( undef, $id ) = _preview( [@$results] );
            _emit( { value => '(' . join( ', ', @parts ) . ')', id => $id } );
        }
        1;
    } or _emit_error($@);
    return;
}

1;
