$stdout.sync = true
mode = ARGV[0].to_s
puts "before-" + %w[ran].join
x = 1    # breakpoint line
puts "after-" + %w[ran].join
if mode == "trap"
  trap("INT") { puts "caught-" + %w[int].join; exit 5 }
end
if mode == "rescue"
  begin
    puts "looping-" + %w[now].join
    loop { sleep 0.2 }
  rescue Interrupt
    puts "rescued-" + %w[int].join
    exit 6
  end
end
if mode == "loop" || mode == "trap"
  puts "looping-" + %w[now].join
  loop { sleep 0.2 }
end
puts "looping-" + %w[now].join if mode == "read"
line = $stdin.gets
puts "got:#{line.chomp}"
exit 7
