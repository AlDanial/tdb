use ExtUtils::MakeMaker;

use strict;
require 5.008001;

# Remember (like I didn't) that WriteMakefile looks at @ARGV,
# so an alternative way to configure a debugging build is:
# perl Makefile.PL DEFINE=-DPADWALKER_DEBUGGING.
my $DEBUGGING = '';
if (@ARGV && $ARGV[0] eq '-d') {
  warn "Configuring a debugging build of PadWalker\n";
  print STDERR <<END;
************************************************************************
* WARNING!  WARNING!  WARNING!  WARNING!  WARNING!  WARNING!  WARNING! *
************************************************************************
You are building PadWalker in debugging mode, which causes it to
print a lot of gnomic information about its internal operation.
The test suite will fail, because this information will confuse
the test harness. You almost certainly do *not* want to do this
unless you're the author of PadWalker (or perhaps just irrepressibly
curious about its internal operation).

END

  $DEBUGGING = '-DPADWALKER_DEBUGGING';
  shift;
}

WriteMakefile(
    'NAME'	=> 'PadWalker',
    'VERSION_FROM' => 'PadWalker.pm', # finds $VERSION
    'LIBS'	=> [''],   # e.g., '-lm' 
    'DEFINE'	=> $DEBUGGING,
    'INC'	=> '',     # e.g., '-I/usr/include/other',
    ($DEBUGGING ? (CCFLAGS => '-Wall -ansi') : ()),
    dist => {TAR => 'env COPYFILE_DISABLE=true tar'},
    MIN_PERL_VERSION => "5.008001",
);
