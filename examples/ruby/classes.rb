#!/usr/bin/env ruby
# Classes and object-oriented programming example.

class User
  attr_reader :name, :age

  def initialize(name, age)
    @name = name
    @age = age
  end

  def greet
    puts "Hi, I'm #{@name} and I'm #{@age} years old."
  end

  def birthday
    @age += 1
    puts "Happy birthday! Now #{@age} years old."
  end
end

if __FILE__ == $0
  user = User.new("Alice", 25)
  user.greet
  user.birthday
  user.greet
end
