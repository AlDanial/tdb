# tdb.rb -- Tdb.breakpoint: drop into tdb at the call site.
#
#   require 'tdb'
#   ...
#   Tdb.breakpoint          # tdb opens here, paused on the next line
#
# The Ruby counterpart of Python's `tdb.breakpoint()`. Needs the debug gem
# (`rdbg`), which tdb's Ruby support needs anyway. Put this file's
# directory on RUBYLIB (`tdb --info` prints it as "tdb.rb dir").
#
# Mechanics: the first call opens the debug gem's own DAP server on an
# ephemeral loopback port (DEBUGGER__.open, nonstop). Every call then
# plants the debug gem's usual one-shot breakpoint on the caller's next
# line (binding.break). When that fires with no client attached, rdbg
# parks the program and announces "wait for debugger connection..."; we
# hear that announcement (see WarnHook) and spawn
# `tdb --lang ruby -r 127.0.0.1:PORT` on this tty, which connects and
# shows the stop. Later calls reuse the connected tdb. If tdb was quit in
# between, the next stop parks again and a fresh tdb is spawned.
#
# Why spawn only once the program is parked, rather than connect first
# and arm second: the debug gem decides REPL-vs-DAP for a new client in
# its greeting, but a session thread that is still running can write REPL
# protocol onto the socket before that decision lands and corrupt the DAP
# handshake. tdb's own Ruby adapter documents the same race and likewise
# connects only after rdbg prints that marker. A parked session thread
# cannot write anything until the greeting is done. And why not a watcher
# thread: rdbg suspends every Ruby thread at a stop, a watcher included.
# The announcement is made by the one thread that keeps running.
#
# No-op when stdin/stdout are not ttys, so it is safe to leave in code
# that sometimes runs headless. Never raises. If tdb cannot be found the
# call warns and does nothing (the program runs on); that check happens
# before arming, because once rdbg has parked the program only a client
# can release it.
#
# `tdb` is found via ENV['TDB'], else on PATH.
module Tdb
  SPAWN_TIMEOUT = 60      # seconds for a spawned tdb to connect
  LINGER_TIMEOUT = 3      # seconds for a quit tdb to leave the terminal

  # rdbg's user-facing announcements (debug gem lib/debug/server.rb).
  PARKED_MSG = 'wait for debugger connection...'
  CONNECTED_MSG = 'Connected.'
  DISCONNECTED_MSG = 'Disconnected.'

  DETACH_TIMEOUT = 5      # seconds for rdbg to finish dropping a client

  @port = nil
  @pid = nil              # the tdb we spawned, if any
  @spawned_at = nil
  @connected = false      # is a client attached right now?
  @connected_once = false # did the tdb we spawned ever connect?
  @detaching = false      # client sent `disconnect`; rdbg not yet done with it

  # Prepended onto DEBUGGER__'s singleton: sees every DEBUGGER__.warn.
  module WarnHook
    def warn(msg)
      Tdb.__rdbg_said(msg.to_s)
      super
    end
  end

  # Prepended onto the debug server's UI object: sees every DAP request.
  # rdbg resumes the program the moment it handles `disconnect`, but only
  # drops the client (its socket, its command queue) once the peer has
  # actually closed the connection. A breakpoint reached in between still
  # finds that half-dead client and its stop is lost (the exhausted
  # command queue reads as `continue`) or never announced. Knowing a
  # disconnect is in flight lets Tdb.breakpoint wait it out before arming.
  module DisconnectHook
    def process_request(req)
      Tdb.__detaching! if req.is_a?(Hash) && req['command'] == 'disconnect'
      super
    end
  end

  class << self
    def breakpoint
      return unless $stdin.tty? && $stdout.tty?
      return unless tdb_command
      return unless ensure_server
      await_detach if @detaching
      # 0 would be this method, 1 the debug gem's own frame; 2 is our
      # caller, whose next line gets the one-shot breakpoint.
      binding.break(up_level: 2)
    end

    # ---- internals ----------------------------------------------------

    # Absolute path of the tdb executable, or nil (after a warning).
    def tdb_command
      cmd = ENV['TDB'] || 'tdb'
      found =
        if cmd.include?(File::SEPARATOR)
          cmd if File.executable?(cmd)
        else
          ENV.fetch('PATH', '').split(File::PATH_SEPARATOR)
             .map { |dir| File.join(dir, cmd) }
             .find { |path| File.executable?(path) }
        end
      warn "Tdb.breakpoint: cannot find #{cmd} (set TDB or add tdb to PATH); continuing" unless found
      found
    end

    def ensure_server
      return true if @port
      begin
        require 'debug/session'
      rescue LoadError
        warn "Tdb.breakpoint: the debug gem is not installed (gem install debug); continuing"
        return false
      end
      require 'tmpdir'
      DEBUGGER__.singleton_class.prepend(WarnHook) unless DEBUGGER__.singleton_class < WarnHook
      port_file = File.join(Dir.tmpdir, "tdb-rb-#{Process.pid}-#{rand(1 << 32)}.port")
      DEBUGGER__.open(host: '127.0.0.1', port: "0:#{port_file}", nonstop: true)
      # The debug gem's accept thread writes the port it got; wait for it.
      deadline = Time.now + 10
      port = nil
      until File.exist?(port_file) && !(port = File.read(port_file).strip).empty?
        if Time.now > deadline
          warn "Tdb.breakpoint: debug server did not report its port; continuing"
          return false
        end
        sleep 0.02
      end
      @port = port.to_i
      ui = DEBUGGER__::SESSION.instance_variable_get(:@ui)
      ui.singleton_class.prepend(DisconnectHook) if ui
      true
    rescue => e
      warn "Tdb.breakpoint: cannot start the debug server: #{e.message}; continuing"
      false
    end

    def __detaching!
      @detaching = true
    end

    # Block (bounded) until rdbg has announced "Disconnected." for the
    # client that asked to leave, so the next stop parks cleanly.
    def await_detach
      deadline = Time.now + DETACH_TIMEOUT
      sleep 0.01 while @detaching && Time.now < deadline
      @detaching = false
    end

    # Called from WarnHook, on whichever rdbg thread made the announcement.
    def __rdbg_said(msg)
      case msg
      when CONNECTED_MSG
        @connected = true
        @connected_once = true if @pid
      when DISCONNECTED_MSG
        @connected = false
        @detaching = false
      when PARKED_MSG
        on_parked
      end
    rescue => e
      warn "Tdb.breakpoint: #{e.class}: #{e.message}"
    end

    # rdbg has stopped the program and is about to block until a client
    # connects. Make sure one is coming.
    def on_parked
      if tdb_alive?
        if @connected_once
          # A quit tdb still tearing its TUI down: give it a moment so two
          # full-screen apps never share the terminal, then replace it.
          deadline = Time.now + LINGER_TIMEOUT
          sleep 0.05 while tdb_alive? && Time.now < deadline
        elsif Time.now - @spawned_at < SPAWN_TIMEOUT
          return # still starting up; it will connect
        else
          warn "Tdb.breakpoint: tdb did not attach within #{SPAWN_TIMEOUT}s; starting another"
        end
      elsif @pid && !@connected_once
        warn "Tdb.breakpoint: tdb exited before attaching; the program is " \
             "suspended by rdbg -- attach with: tdb --lang ruby -r 127.0.0.1:#{@port}"
        @pid = nil
        return
      end
      spawn_tdb
    end

    def tdb_alive?
      return false unless @pid
      begin
        Process.waitpid(@pid, Process::WNOHANG).nil?
      rescue Errno::ECHILD
        # Not ours to reap (the program traps CHLD): fall back to a probe.
        begin
          Process.kill(0, @pid)
          true
        rescue Errno::ESRCH
          false
        end
      end
    end

    def spawn_tdb
      cmd = [tdb_command, '--lang', 'ruby', '-r', "127.0.0.1:#{@port}"]
      return false unless cmd[0]
      @pid = Process.spawn(*cmd, in: $stdin, out: $stdout, err: $stderr)
      @spawned_at = Time.now
      @connected_once = false
      true
    rescue SystemCallError => e
      warn "Tdb.breakpoint: cannot start #{cmd[0]}: #{e.message}"
      @pid = nil
      false
    end
  end
end
