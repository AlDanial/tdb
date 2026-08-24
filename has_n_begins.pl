#!/usr/bin/env perl
use strict;
use warnings;


BEGIN {
    my $a = 10;
    my %b;
    print "This is the BEGIN block. The value of a is: $a\n";
    $a++; 
}

BEGIN {
    print "This is another BEGIN block\n";
}

my $b = 20;

END {
    print "This is the END block. The value of b is: $b\n";
}
