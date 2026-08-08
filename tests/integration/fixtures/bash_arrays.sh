declare -a fruits=(apple banana cherry)
declare -A prices=([apple]=3 [banana]=1)
greeting="hello"
echo "${fruits[1]} ${prices[apple]} $greeting"
