#!/usr/bin/env ruby
# Factorial example with recursive function.

def factorial(n)
  if n <= 1
    1
  else
    n * factorial(n - 1)
  end
end

if __FILE__ == $0
  puts "factorial(5) = #{factorial(5)}"
  puts "factorial(10) = #{factorial(10)}"
end
