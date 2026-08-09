declare -a fruits=(apple banana cherry)
declare -A prices=([apple]=3 [banana]=1)
greeting="hello"
HISTORY="not the bash history builtin"
EPOCH_START=$(date +%s)
SHELLCHECK_OPTS="-e SC2034"
FUNCNEST=50
echo "${fruits[1]} ${prices[apple]} $greeting"
