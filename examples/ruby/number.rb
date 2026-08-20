# 1. 変数の宣言（テスト用データ）
numbers = [12, 45, 7, 23, 56, 89, 34]
threshold = 30

count_over_threshold = 0
total_sum = 0

puts '=== デバッグテスト開始 ==='
puts "対象の配列: #{numbers.inspect}"
puts "しきい値: #{threshold}"
puts '------------------------'

# 2. ループ処理と条件分岐
numbers.each_with_index do |num, index|
  # 各ループの開始時に変数の状態を確認できます
  puts "[インデックス: #{index}] 現在の数値: #{num}"

  if num > threshold
    puts "  -> しきい値 #{threshold} より大きいです。"
    count_over_threshold += 1
  else
    puts "  -> しきい値 #{threshold} 以下です。"
  end

  total_sum += num
end

# 3. 最終結果の画面出力
puts '------------------------'
puts '処理が完了しました。'
puts "しきい値を超えた回数: #{count_over_threshold} 回"
puts "すべての数値の合計値: #{total_sum}"
