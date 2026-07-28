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

# Expandable-ref stash: id -> ref. Cleared at each stop (location()).
our %REG;
our $NEXT_ID = 1;

sub _out {
    my ($line) = @_;
    my $fh = ( defined fileno(*DB::OUT) ) ? \*DB::OUT : \*STDOUT;
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

1;
