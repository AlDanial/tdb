/* before/after-ran markers are built at run time so the TUI's source
   pane cannot satisfy the test's output asserts. */
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static void on_int(int sig) {
    (void)sig;
    write(1, "caught-int\n", 11);
    _exit(5);
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "";
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("%s%s\n", "before", "-ran");
    if (strcmp(mode, "handler") == 0)
        signal(SIGINT, on_int);
    if (strcmp(mode, "loop") == 0 || strcmp(mode, "handler") == 0) {
        printf("%s%s\n", "looping", "-now");
        for (;;)
            sleep(1);
    }
    printf("%s%s\n", "after-", "ran");
    return 7;
}
