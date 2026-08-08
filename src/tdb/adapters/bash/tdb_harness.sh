# tdb's bash debug harness, sourced via BASH_ENV before the debuggee's
# first line. Every identifier here is __tdb_/__TDB_-prefixed; the
# debuggee's namespace is otherwise untouched.
#
# Correctness rules (spec: 2026-08-08-bash-dap-design.md):
#  - the DEBUG trap must finish with status 0 on every resume path
#    (extdebug: nonzero skips the debuggee's next command)
#  - everything must survive set -euo pipefail in the debuggee
#  - locals/eval run INLINE in the trap string (scope!)

# Child bash processes inherit exported vars until we unset BASH_ENV,
# but never the pipe FDs; refuse to arm without them.
[[ -n ${__TDB_CMD_FD:-} && -n ${__TDB_RESP_FD:-} && -n ${__TDB_TMP:-} ]] || return 0

if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
    printf 'tdb: bash >= 4.4 is required to debug (this is bash %s)\n' \
        "$BASH_VERSION" >&2
    exit 2
fi

unset BASH_ENV
shopt -s extdebug
set -o functrace

declare -A __tdb_bp=()      # "canonical:line" -> condition ("" = always)
declare -A __tdb_canon=()   # raw source path -> canonical path
__tdb_mode=continue         # step|next|finish|continue
__tdb_depth=0               # ${#FUNCNAME[@]} when the last resume was armed
__tdb_entry_pending=1       # first step-stop reports reason "entry"
__tdb_pause=0
__tdb_reason=entry
__tdb_cur_file= __tdb_cur_line=0 __tdb_cur_depth=0
__tdb_rc=0

__tdb_send() { printf '%s\n' "$1" >&"$__TDB_RESP_FD"; return 0; }

__tdb_b64() {   # stdout: base64 of $1; "-" for empty
    if [[ -z $1 ]]; then printf -- '-'; else printf '%s' "$1" | base64 | tr -d '\n'; fi
    return 0
}

__tdb_unb64() { # decodes $1 into __tdb_dec
    if [[ ${1:--} == - ]]; then __tdb_dec=
    else __tdb_dec=$(printf '%s' "$1" | base64 -d); fi
    return 0
}

__tdb_canonical() {  # canonicalize $1 -> __tdb_cpath (cached; realpath(dir)+basename)
    local __tdb_p=$1 __tdb_d
    if [[ -n ${__tdb_canon[$__tdb_p]:-} ]]; then
        __tdb_cpath=${__tdb_canon[$__tdb_p]}; return 0
    fi
    if [[ $__tdb_p == */* ]]; then __tdb_d=${__tdb_p%/*}; else __tdb_d=.; fi
    __tdb_d=$(cd -- "$__tdb_d" 2>/dev/null && pwd -P) || __tdb_d=
    if [[ -n $__tdb_d ]]; then __tdb_cpath=$__tdb_d/${__tdb_p##*/}
    else __tdb_cpath=$__tdb_p; fi
    __tdb_canon[$__tdb_p]=$__tdb_cpath
    return 0
}

__tdb_read_cmd() {  # blocks; fills __tdb_cmd/__tdb_a1/__tdb_a2/__tdb_a3
    local __tdb_line
    IFS= read -r -u "$__TDB_CMD_FD" __tdb_line || return 1
    __tdb_a1= __tdb_a2= __tdb_a3=
    read -r __tdb_cmd __tdb_a1 __tdb_a2 __tdb_a3 <<< "$__tdb_line"
    return 0
}

__tdb_apply_cmd() {  # setbp/clearbp/clearall/pause from __tdb_cmd/__tdb_a*
    case $__tdb_cmd in
    setbp)
        __tdb_unb64 "$__tdb_a1"; local __tdb_f=$__tdb_dec
        __tdb_unb64 "$__tdb_a3"
        __tdb_bp["$__tdb_f:$__tdb_a2"]=$__tdb_dec ;;
    clearbp)
        __tdb_unb64 "$__tdb_a1"
        unset "__tdb_bp[$__tdb_dec:$__tdb_a2]" 2>/dev/null || true ;;
    clearall) __tdb_bp=() ;;
    pause) __tdb_pause=1 ;;
    esac
    return 0
}

__tdb_drain() {  # apply queued commands without blocking (running fast path)
    # -t 0 probes for available input without consuming; the explicit
    # varname keeps it from clobbering the debuggee's $REPLY
    while read -t 0 -u "$__TDB_CMD_FD" __tdb_probe; do
        __tdb_read_cmd || return 0
        __tdb_apply_cmd
    done
    return 0
}

__tdb_stack() {  # $1 = harness frames to skip; stdout: func|canon|line per frame
    local __tdb_skip=$1 __tdb_i __tdb_out
    __tdb_canonical "$__tdb_cur_file"
    __tdb_out="${FUNCNAME[$__tdb_skip]:-main}|$__tdb_cpath|$__tdb_cur_line"
    for (( __tdb_i = __tdb_skip; __tdb_i < ${#FUNCNAME[@]} - 1; __tdb_i++ )); do
        __tdb_canonical "${BASH_SOURCE[__tdb_i + 1]:-}"
        __tdb_out+=$'\n'"${FUNCNAME[__tdb_i + 1]:-main}|$__tdb_cpath|${BASH_LINENO[__tdb_i]:-0}"
    done
    printf '%s' "$__tdb_out"
    return 0
}

__tdb_dispatch() {  # scope-independent commands, acked (stopped/config phase)
    case $__tdb_cmd in
    stack)   __tdb_send "ok $(__tdb_b64 "$(__tdb_stack 2)")" ;;
    globals) __tdb_send "ok $(__tdb_b64 "$(declare -p 2>/dev/null || true)")" ;;
    setbp|clearbp|clearall|pause)
             __tdb_apply_cmd; __tdb_send "ok -" ;;
    *)       __tdb_send "err $(__tdb_b64 "unknown command: $__tdb_cmd")" ;;
    esac
    return 0
}

__tdb_should_stop() {  # args: file line depth; 0 = stop (fills __tdb_cur_*/__tdb_reason)
    local __tdb_f=$1 __tdb_l=$2 __tdb_d=$3
    (( BASH_SUBSHELL )) && return 1
    __tdb_drain
    if (( __tdb_pause )); then
        __tdb_pause=0; __tdb_reason=pause
    elif [[ $__tdb_mode == step ]]; then
        if (( __tdb_entry_pending )); then __tdb_reason=entry; __tdb_entry_pending=0
        else __tdb_reason=step; fi
    elif [[ $__tdb_mode == next ]] && (( __tdb_d <= __tdb_depth )); then
        __tdb_reason=step
    elif [[ $__tdb_mode == finish ]] && (( __tdb_d < __tdb_depth )); then
        __tdb_reason=step
    else
        (( ${#__tdb_bp[@]} )) || return 1
        __tdb_canonical "$__tdb_f"
        [[ -v "__tdb_bp[$__tdb_cpath:$__tdb_l]" ]] || return 1
        local __tdb_cond=${__tdb_bp[$__tdb_cpath:$__tdb_l]}
        if [[ -n $__tdb_cond ]]; then
            eval "$__tdb_cond" >/dev/null 2>&1 || return 1
        fi
        __tdb_reason=breakpoint
    fi
    __tdb_entry_pending=0
    __tdb_cur_file=$__tdb_f __tdb_cur_line=$__tdb_l __tdb_cur_depth=$__tdb_d
    return 0
}

__tdb_notify() {
    __tdb_canonical "$__tdb_cur_file"
    __tdb_send "stopped $__tdb_reason $(__tdb_b64 "$__tdb_cpath") $__tdb_cur_line"
    return 0
}

# ---- config phase: adapter sets breakpoints, then sends a resume command.
__tdb_cur_file=$0
__tdb_send "ready $$ $BASH_VERSION"
while __tdb_read_cmd; do
    case $__tdb_cmd in
    step|next|finish|continue)
        __tdb_mode=$__tdb_cmd __tdb_depth=0
        break ;;
    *) __tdb_dispatch ;;
    esac
done

# ---- the DEBUG trap. locals/eval are INLINE here on purpose: a helper
# function would push its own scope and local -p / eval would see the
# wrong frame. The trailing `:` forces trap status 0 (extdebug!).
trap '__tdb_should_stop "${BASH_SOURCE[0]:-$0}" "$LINENO" "${#FUNCNAME[@]}" && {
    __tdb_notify
    while __tdb_read_cmd; do
        case $__tdb_cmd in
        locals)
            __tdb_send "ok $(__tdb_b64 "$(local -p 2>/dev/null || true)")" ;;
        eval)
            __tdb_unb64 "$__tdb_a1"
            __tdb_rc=0
            eval "$__tdb_dec" >"$__TDB_TMP/eval.out" 2>&1 || __tdb_rc=$?
            __tdb_send "ok $(__tdb_b64 "rc=$__tdb_rc
$(< "$__TDB_TMP/eval.out")")" ;;
        step|next|finish|continue)
            __tdb_mode=$__tdb_cmd __tdb_depth=$__tdb_cur_depth
            break ;;
        *) __tdb_dispatch ;;
        esac
    done
}; :' DEBUG
