/* Build: cc -g -O0 -pthread pi_digits.c -o pi_digits_c
 * Run:   ./pi_digits_c -r 2 -d 20 -t 2 -p
 */
#define _POSIX_C_SOURCE 200809L
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct { int count; char *digits; double elapsed; } Job;

static double seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* Bounded base-10 spigot. The first emitted character is a carry placeholder. */
static char *pi_digits(int count) {
    int size = count * 10 / 3 + 1;
    int *remainders = malloc((size_t)size * sizeof *remainders);
    char *out = malloc((size_t)count + 3);
    if (!remainders || !out) { perror("malloc"); exit(1); }
    for (int i = 0; i < size; ++i) remainders[i] = 2;
    int used = 0, pending = 0, previous = 0;
    for (int step = 0; step <= count; ++step) {
        long long quotient = 0;
        for (int i = size; i >= 1; --i) {
            long long value = 10LL * remainders[i - 1] + quotient * i;
            remainders[i - 1] = (int)(value % (2 * i - 1));
            quotient = value / (2 * i - 1);
        }
        remainders[0] = (int)(quotient % 10);
        quotient /= 10;
        if (quotient == 9) ++pending;
        else if (quotient == 10) {
            out[used++] = (char)('0' + previous + 1);
            while (pending--) out[used++] = '0';
            pending = 0; previous = 0;
        } else {
            out[used++] = (char)('0' + previous);
            while (pending--) out[used++] = '9';
            pending = 0; previous = (int)quotient;
        }
    }
    out[used++] = (char)('0' + previous);
    while (pending--) out[used++] = '9';
    out[count + 1] = '\0';
    memmove(out, out + 1, (size_t)count + 1);
    free(remainders);
    return out;
}

static void *worker(void *arg) {
    Job *job = arg;
    double start = seconds();
    job->digits = pi_digits(job->count);
    job->elapsed = seconds() - start;
    return NULL;
}

static int positive(const char *value) {
    char *end;
    long number = strtol(value, &end, 10);
    if (!*value || *end || number < 1 || number > 100000) {
        fprintf(stderr, "expected a positive integer: %s\n", value); exit(2);
    }
    return (int)number;
}

int main(int argc, char **argv) {
    int runs = 1, count = 10, threads = 1, print = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "-p") || !strcmp(argv[i], "--print")) print = 1;
        else if (i + 1 < argc && (!strcmp(argv[i], "-r") || !strcmp(argv[i], "--n-runs"))) runs = positive(argv[++i]);
        else if (i + 1 < argc && (!strcmp(argv[i], "-d") || !strcmp(argv[i], "--n-digits"))) count = positive(argv[++i]);
        else if (i + 1 < argc && (!strcmp(argv[i], "-t") || !strcmp(argv[i], "--n-threads"))) threads = positive(argv[++i]);
        else { fprintf(stderr, "usage: %s [-r runs] [-d digits] [-t threads] [-p]\n", argv[0]); return 2; }
    }
    for (int run = 1; run <= runs; ++run) {
        double run_start = seconds();
        Job *jobs = calloc((size_t)threads, sizeof *jobs);
        pthread_t *ids = malloc((size_t)threads * sizeof *ids);
        if (!jobs || !ids) { perror("malloc"); return 1; }
        for (int j = 0; j < threads; ++j) {
            jobs[j].count = count;
            if (pthread_create(&ids[j], NULL, worker, &jobs[j])) { perror("pthread_create"); return 1; }
        }
        for (int j = 0; j < threads; ++j) {
            pthread_join(ids[j], NULL);
            if (threads > 1) printf("Run %3d/%3d, thread %3d/%3d completed in %.6f seconds.\n", run, runs, j + 1, threads, jobs[j].elapsed);
            else printf("Run %3d/%3d completed in %.6f seconds.\n", run, runs, jobs[j].elapsed);
        }
        if (threads > 1) printf("== Run %3d/%3d completed in %.6f seconds.\n", run, runs, seconds() - run_start);
        if (run == runs && print) puts(jobs[0].digits);
        for (int j = 0; j < threads; ++j) free(jobs[j].digits);
        free(ids); free(jobs);
    }
    return 0;
}
