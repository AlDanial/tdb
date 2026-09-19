#!/usr/bin/env ruby
require 'optparse'

def pi_digits(count)
  a = Array.new(count * 10 / 3 + 1, 2)
  out = +''
  previous = pending = 0
  (count + 1).times do
    q = 0
    a.length.downto(1) do |i|
      x = 10 * a[i - 1] + q * i
      a[i - 1] = x % (2 * i - 1)
      q = x / (2 * i - 1)
    end
    a[0] = q % 10
    q /= 10
    if q == 9
      pending += 1
    elsif q == 10
      out << (previous + 1).to_s << '0' * pending
      previous = pending = 0
    else
      out << previous.to_s << '9' * pending
      previous, pending = q, 0
    end
  end
  out << previous.to_s << '9' * pending
  out[1, count]
end

options = { runs: 1, digits: 10, threads: 1, print: false }
OptionParser.new do |parser|
  parser.on('-r', '--n-runs N', Integer) { |n| options[:runs] = n }
  parser.on('-d', '--n-digits N', Integer) { |n| options[:digits] = n }
  parser.on('-t', '--n-threads N', Integer) { |n| options[:threads] = n }
  parser.on('-p', '--print') { options[:print] = true }
end.parse!
abort 'counts must be positive' if options.values_at(:runs, :digits, :threads).any? { |n| n < 1 }

1.upto(options[:runs]) do |run|
  run_start = Process.clock_gettime(Process::CLOCK_MONOTONIC)
  workers = Array.new(options[:threads]) do
    Thread.new do
      start = Process.clock_gettime(Process::CLOCK_MONOTONIC)
      digits = pi_digits(options[:digits])
      [digits, Process.clock_gettime(Process::CLOCK_MONOTONIC) - start]
    end
  end
  results = workers.map(&:value)
  results.each_with_index do |(_, elapsed), j|
    if options[:threads] > 1
      printf "Run %3d/%3d, thread %3d/%3d completed in %.6f seconds.\n", run, options[:runs], j + 1, options[:threads], elapsed
    else
      printf "Run %3d/%3d completed in %.6f seconds.\n", run, options[:runs], elapsed
    end
  end
  if options[:threads] > 1
    printf "== Run %3d/%3d completed in %.6f seconds.\n", run, options[:runs], Process.clock_gettime(Process::CLOCK_MONOTONIC) - run_start
  end
  puts results[0][0] if run == options[:runs] && options[:print]
end
