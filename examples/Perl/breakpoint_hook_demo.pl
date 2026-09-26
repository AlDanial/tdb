#!/usr/bin/env perl
# Demo: drop into tdb at a specific line via Devel::TdbRemote::breakpoint().
#
# Run it directly (not under tdb), with Devel::TdbRemote on @INC:
#   PERL5LIB=$(tdb --info | sed -n 's/.*Devel::TdbRemote dir *//p') perl breakpoint_hook_demo.pl
use Devel::TdbRemote;    # first line, so everything below is debuggable
use strict;
use warnings;

sub compute {
    my ($n) = @_;
    my $total = 0;
    my @local_list = ( 1, 2, 3, 4, 5 );
    $total += $_ for 0 .. $n - 1;
    Devel::TdbRemote::breakpoint();    # tdb opens here; inspect $total and @local_list
    return $total;
}

my $result = compute(10);
print "result = $result\n";
