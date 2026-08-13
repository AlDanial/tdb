#!/usr/bin/tcsh -f
set name = original
set items = (one "two words")
setenv TCSH_DAP_SCOPE_VALUE "environment value"
alias greeting 'echo hello world'
echo "scope fixture ready"
exit 0
