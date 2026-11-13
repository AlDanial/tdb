/* before/after-ran markers are built at run time so the TUI's source
   pane cannot satisfy the test's output asserts. */
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int handler_exit = 5;
static int handler_delay = 0;

static void on_int(int sig) {
    (void)sig;
    write(1, "caught-int\n", 11);
    if (handler_delay)
        sleep(handler_delay);
    _exit(handler_exit);
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "";
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("%s%s\n", "before", "-ran");
    /* "handler": exit 5; "handler0"/"handler2": exit 0/2 from the handler */
    /* "slow0": like handler0 but cleans up for 3s first */
    int handled = strncmp(mode, "handler", 7) == 0 || strcmp(mode, "slow0") == 0;
    if (strcmp(mode, "slow0") == 0) {
        handler_exit = 0;
        handler_delay = 3;
    }
    if (handled) {
        if (mode[0] == 'h' && mode[7] != '\0')
            handler_exit = mode[7] - '0';
        signal(SIGINT, on_int);
    }
    if (strcmp(mode, "loop") == 0 || handled) {
        printf("%s%s\n", "looping", "-now");
        for (;;)
            sleep(1);
    }
    printf("%s%s\n", "after-", "ran");
    return 7;
}
