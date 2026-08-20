#!/usr/bin/env ruby
# E2E entry point: boot the bundled Rails app, print a marker line, and exit
# naturally. Driven by tests/integration/test_ruby_rails_bundler.py through
# the rdbg bridge's `useBundler` launch path (`bundle exec ruby bin/e2e.rb`).
require_relative "../config/environment"

puts "rails booted"