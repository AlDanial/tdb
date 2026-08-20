#!/usr/bin/env ruby
# Simple "Hello, World!" example for tdb Ruby debugging.

def greet(name)
  puts "Hello, #{name}!"
end

if __FILE__ == $0
  greet("World")
  greet("Ruby")
end
