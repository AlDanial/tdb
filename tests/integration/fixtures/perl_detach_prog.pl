use strict; use warnings;
$| = 1;
my $mode = shift // '';
print "before-" . join('', 'ran'), "\n";
my $x = 1;    # breakpoint line
print "after-" . join('', 'ran'), "\n";
if ($mode eq 'trap') {
    $SIG{INT} = sub { print "caught-" . join('', 'int'), "\n"; exit 5 };
}
if ($mode eq 'loop' || $mode eq 'trap') {
    print "looping-" . join('', 'now'), "\n";
    while (1) { select(undef, undef, undef, 0.2); }
}
print "looping-" . join('', 'now'), "\n" if $mode eq 'read';
my $line = <STDIN>;
chomp $line;
print "got:$line\n";
exit 7;
