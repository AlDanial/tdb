def inner(n)
  m = n * 2
  m + 1
end

def outer(k)
  inner(k) + inner(k + 1)
end

total = 0
[1, 2, 3].each do |i|
  total += outer(i)
end
puts total
