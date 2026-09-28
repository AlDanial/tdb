/* tdb.h -- live breakpoint hook for C and C++ programs debugged with tdb.
 *
 *   #include "tdb.h"
 *   ...
 *   tdb_breakpoint();   // tdb opens here, paused on the next line
 *
 * Run the program directly, not under tdb. When the call is reached it
 * starts `tdb --lang cpp -a <pid> --no-pause-on-attach` on the program's
 * terminal (the `tdb` on PATH, or the one named by $TDB), waits for tdb's
 * gdb or lldb to attach, then stops in tdb_breakpoint_stop(); tdb steps
 * out to the line after the call. Later calls reuse the running tdb;
 * quitting tdb (Ctrl+q) detaches and the program runs on. The call is a
 * no-op when stdin or stdout is not a terminal, and it warns on stderr
 * and continues when tdb cannot be found, exits early, or fails to attach
 * within TDB_ATTACH_TIMEOUT_MS. Compile with -g -O0 to keep locals
 * inspectable.
 *
 * Linux only: attaching needs ptrace, granted to tdb's debugger with
 * PR_SET_PTRACER (Yama ptrace_scope=1). Elsewhere the hook warns once and
 * returns. Header-only; the functions are weak so several translation
 * units may include it. Compile-time knobs: TDB_ATTACH_TIMEOUT_MS (60000),
 * TDB_LINGER_TIMEOUT_MS (3000), TDB_POLL_MS (50).
 *
 * Find this header with `tdb --info` ("tdb.h dir") and pass that
 * directory with -I.
 *
 * Under -std=c99 / -std=c++98 and stricter, glibc hides posix_spawnp(),
 * nanosleep(), and the `environ` declaration unless a wide-enough feature
 * test macro is already set. This header defines _GNU_SOURCE itself, but
 * only takes effect if nothing in the translation unit has included a
 * system header yet -- so #include "tdb.h" before any system header
 * (as in the example above), or #define _GNU_SOURCE yourself before your
 * own first #include if tdb.h cannot be first.
 */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#ifndef TDB_H
#define TDB_H

#ifndef TDB_ATTACH_TIMEOUT_MS
#define TDB_ATTACH_TIMEOUT_MS 60000
#endif
#ifndef TDB_LINGER_TIMEOUT_MS
#define TDB_LINGER_TIMEOUT_MS 3000
#endif
#ifndef TDB_POLL_MS
#define TDB_POLL_MS 50
#endif

#if defined(__GNUC__) || defined(__clang__)
#define TDB_HOOK_FN __attribute__((weak, noinline, used))
#else
#define TDB_HOOK_FN static
#endif

#ifdef __cplusplus
extern "C" {
#endif

#if defined(__GNUC__) || defined(__clang__)
/* Prototypes only for the weak, externally visible definitions; elsewhere
 * the functions are static (one private copy per translation unit) and a
 * non-static prototype would conflict with them. */
void tdb_breakpoint(void);
void tdb_breakpoint_lang(const char *lang);
void tdb_breakpoint_stop(void);
#endif

/* The function tdb's hidden breakpoint lands in. Must stay a real,
 * externally visible, never-inlined symbol named tdb_breakpoint_stop. */
TDB_HOOK_FN void tdb_breakpoint_stop(void) {
#if defined(__GNUC__) || defined(__clang__)
    __asm__ volatile("" ::: "memory");
#endif
}

#if defined(__linux__)

#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

static pid_t tdb__child = 0;         /* last tdb we spawned, 0 if none/reaped */
static int tdb__tracer_allowed = 0;
static int tdb__warned_stale = 0;    /* "previous tdb still running" said */

static void tdb__warn(const char *msg) {
    fprintf(stderr, "tdb_breakpoint: %s; continuing without a debugger\n", msg);
}

static void tdb__sleep_ms(long ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (ms % 1000) * 1000000L;
    nanosleep(&ts, NULL);
}

/* Pid of the tracer attached to THIS thread (gdb/lldb attach thread by
 * thread; /proc/self/status describes only the main thread). */
static int tdb__tracer_pid(void) {
    FILE *f = fopen("/proc/thread-self/status", "r");
    char line[256];
    int pid = 0;
    if (f == NULL) return 0;
    while (fgets(line, sizeof line, f) != NULL) {
        if (strncmp(line, "TracerPid:", 10) == 0) {
            pid = (int)strtol(line + 10, NULL, 10);
            break;
        }
    }
    fclose(f);
    return pid;
}

/* 1 if the last spawned tdb has exited (and reap it), else 0. */
static int tdb__child_exited(void) {
    int status;
    pid_t r;
    if (tdb__child == 0) return 1;
    r = waitpid(tdb__child, &status, WNOHANG);
    if (r == tdb__child || (r == -1 && errno == ECHILD)) {
        tdb__child = 0;
        return 1;
    }
    return 0;
}

static void tdb__wait_child_exit(long timeout_ms) {
    long waited = 0;
    while (!tdb__child_exited() && waited < timeout_ms) {
        tdb__sleep_ms(TDB_POLL_MS);
        waited += TDB_POLL_MS;
    }
}

static int tdb__spawn(const char *lang) {
    const char *name = getenv("TDB");
    char pidstr[32];
    char *argv[7];
    int rc;
    if (name == NULL || *name == '\0') name = "tdb";
    snprintf(pidstr, sizeof pidstr, "%d", (int)getpid());
    argv[0] = (char *)name;
    argv[1] = (char *)"--lang";
    argv[2] = (char *)lang;
    argv[3] = (char *)"-a";
    argv[4] = pidstr;
    argv[5] = (char *)"--no-pause-on-attach";
    argv[6] = NULL;
    /* posix_spawnp is safe to call in a multithreaded program, unlike
     * fork(); stdin, stdout, and stderr are all inherited so tdb can draw
     * its TUI on the program's terminal and its own diagnostics land
     * wherever the program's stderr already goes. */
    rc = posix_spawnp(&tdb__child, name, NULL, NULL, argv, environ);
    if (rc != 0) {
        char msg[256];
        snprintf(msg, sizeof msg, "cannot start %s (%s); install tdb or point $TDB at it",
                 name, strerror(rc));
        tdb__warn(msg);
        tdb__child = 0;
        return 0;
    }
    return 1;
}

/* 1 when a debugger is attached to this thread and stopping is safe.
 * Caller holds the hook lock (GNU path). */
static int tdb__ensure_debugger_locked(const char *lang) {
    long waited = 0;
    if (!tdb__tracer_allowed) {
        /* Let a non-ancestor (tdb's gdb/lldb-dap) attach under Yama. */
        prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY, 0, 0, 0);
        tdb__tracer_allowed = 1;
    }
    if (tdb__tracer_pid() != 0) return 1;
    /* A tdb spawned earlier may still be leaving the terminal. */
    tdb__wait_child_exit(TDB_LINGER_TIMEOUT_MS);
    if (!tdb__child_exited()) {
        /* Still alive: most likely a tdb whose attach was refused. Do not
         * stack a second TUI on the same terminal over it. */
        if (!tdb__warned_stale) {
            tdb__warn("previous tdb still running (attach may be blocked; "
                      "see /proc/sys/kernel/yama/ptrace_scope); quit it with "
                      "Ctrl+q before the next tdb_breakpoint()");
            tdb__warned_stale = 1;
        }
        return 0;
    }
    if (!tdb__spawn(lang)) return 0;
    while (tdb__tracer_pid() == 0) {
        if (tdb__child_exited()) {
            tdb__warn("tdb exited before attaching");
            return 0;
        }
        if (waited >= TDB_ATTACH_TIMEOUT_MS) {
            tdb__warn("tdb did not attach in time (is ptrace allowed? "
                      "see /proc/sys/kernel/yama/ptrace_scope)");
            return 0;
        }
        tdb__sleep_ms(TDB_POLL_MS);
        waited += TDB_POLL_MS;
    }
    return 1;
}

#if defined(__GNUC__) || defined(__clang__)
/* Serializes tdb__ensure_debugger across threads so two threads reaching
 * the hook before the debugger attaches spawn one tdb, not two: the second
 * waits here, then finds the first one's tracer attached and stops too. A
 * spin-sleep on an atomic flag keeps the header free of a pthread
 * dependency. Without GCC/Clang builtins there is no lock. */
static int tdb__lock = 0;
#define TDB__LOCK() \
    while (__atomic_exchange_n(&tdb__lock, 1, __ATOMIC_ACQUIRE)) tdb__sleep_ms(TDB_POLL_MS)
#define TDB__UNLOCK() __atomic_store_n(&tdb__lock, 0, __ATOMIC_RELEASE)
#else
#define TDB__LOCK() ((void)0)
#define TDB__UNLOCK() ((void)0)
#endif

static int tdb__ensure_debugger(const char *lang) {
    int ok;
    if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) return 0;
    TDB__LOCK();
    ok = tdb__ensure_debugger_locked(lang);
    TDB__UNLOCK();
    return ok;
}

TDB_HOOK_FN void tdb_breakpoint_lang(const char *lang) {
    if (tdb__ensure_debugger(lang)) {
        tdb_breakpoint_stop();
    }
}

#else /* not Linux */

#include <stdio.h>

TDB_HOOK_FN void tdb_breakpoint_lang(const char *lang) {
    static int warned = 0;
    (void)lang;
    if (!warned) {
        fprintf(stderr, "tdb_breakpoint: live attach is supported on Linux only; continuing\n");
        warned = 1;
    }
}

#endif /* __linux__ */

TDB_HOOK_FN void tdb_breakpoint(void) {
    tdb_breakpoint_lang("cpp");
}

#ifdef __cplusplus
}
#endif

#endif /* TDB_H */
