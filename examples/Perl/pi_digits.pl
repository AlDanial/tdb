#!/usr/bin/env perl
use strict;
use warnings;
use Getopt::Long;
use Time::HiRes qw(time);

my ($runs, $count, $print) = (1, 10, 0);
GetOptions('r|n-runs=i' => \$runs, 'd|n-digits=i' => \$count, 'p|print' => \$print)
    or die "usage: $0 [-r runs] [-d digits] [-p]\n";
die "counts must be positive\n" if $runs < 1 || $count < 1;

sub pi_digits {
    my ($count) = @_;
    my @a = (2) x (int($count * 10 / 3) + 1);
    my ($out, $previous, $pending) = ('', 0, 0);
    for (0 .. $count) {
        my $q = 0;
        for (my $i = scalar @a; $i >= 1; --$i) {
            my $x = 10 * $a[$i - 1] + $q * $i;
            $a[$i - 1] = $x % (2 * $i - 1);
            $q = int($x / (2 * $i - 1));
        }
        $a[0] = $q % 10;
        $q = int($q / 10);
        if ($q == 9) { ++$pending; }
        elsif ($q == 10) { $out .= ($previous + 1) . ('0' x $pending); ($previous, $pending) = (0, 0); }
        else { $out .= $previous . ('9' x $pending); ($previous, $pending) = ($q, 0); }
    }
    $out .= $previous . ('9' x $pending);
    return substr($out, 1, $count);
}

for my $run (1 .. $runs) {
    my $start = time();
    my $digits = pi_digits($count);
    printf "Run %3d/%3d completed in %.6f seconds.\n", $run, $runs, time() - $start;
    print "$digits\n" if $run == $runs && $print;
}
