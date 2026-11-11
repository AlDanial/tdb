# tdb_rdbg_ready.rb -- tell tdb's Ruby adapter when rdbg is ready for it.
#
# Loaded via RUBYOPT=-rtdb_rdbg_ready (RUBYLIB points here) into the
# rdbg launched for a `console: externalTerminal` session, where the
# adapter cannot read rdbg's stderr. Connecting before rdbg's session
# thread parks on "wait for debugger connection..." hits the debug gem's
# REPL-vs-DAP greeting race (see _RDBG_READY_MARKER in server.py); a
# proxy-owned launch waits for that line on stderr, and this hook gives
# the terminal launch the same signal: when the debug gem warns the
# marker, create the file named by TDB_RDBG_READY_FILE.
#
# The hook must load before the debug gem (rdbg puts its own -r after
# the user's RUBYOPT) and must not define DEBUGGER__ itself: the gem's
# entry points test `defined?(DEBUGGER__)` to see whether session.rb
# loaded. So a TracePoint waits for the gem to open the module and
# prepends onto its singleton then, before `DEBUGGER__.warn` exists
# (prepend order does not depend on definition order). Never raises.
if (path = ENV['TDB_RDBG_READY_FILE']) && !path.empty?
  module TdbRdbgReady
    MARKER = 'wait for debugger connection...'

    class << self
      attr_accessor :path
    end

    def warn(msg)
      p = TdbRdbgReady.path
      if p && msg.to_s.include?(MARKER)
        TdbRdbgReady.path = nil
        ENV.delete('TDB_RDBG_READY_FILE')
        begin
          File.open(p, 'w') {}
        rescue StandardError
          nil
        end
      end
      super
    end
  end
  TdbRdbgReady.path = path

  TracePoint.new(:class) do |tp|
    mod = tp.self
    if mod.is_a?(Module) && mod.name == 'DEBUGGER__'
      tp.disable
      mod.singleton_class.prepend(TdbRdbgReady)
    end
  end.enable
end
