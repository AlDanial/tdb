"""Bundled DAP adapter for tcsh (python -m tdb.adapters.tcsh).

Ported from the standalone tcsh-dap project: debugs .csh/.tcsh scripts
with a stock tcsh by running instrumented temporary copies and
coordinating stops through private POSIX FIFOs. Requires Python 3.11+
and Unix (the language profile gates both before spawning this module).
"""
