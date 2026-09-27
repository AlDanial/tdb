#!/usr/bin/env ruby
# Demo: drop into tdb at a specific line via Tdb.breakpoint.
#
# Run it directly (not under tdb), with tdb.rb on the load path:
#   RUBYLIB=$(tdb --info | sed -n 's/.*tdb.rb dir *: *//p') ruby breakpoint_hook_demo.rb
require 'tdb'

def compute(n)
  total = 0
  local_list = [1, 2, 3, 4, 5]
  n.times { |i| total += i }
  Tdb.breakpoint # tdb opens here; inspect total and local_list
  total
end

result = compute(10)
puts "result = #{result}"
