/*
 * fl_filedlg_stub_launch.c - TEST-ONLY stand-in for fl-launch.exe used by
 * fl-filedlg-unit.exe under Wine. Prints one absolute Unix path (or exits 1
 * for cancel / 2 for error) so fl_filedlg.c's Show() path is exercised.
 * Never installed or shipped.
 *
 * Named fl-launch.exe next to fl-filedlg.dll. Behaviour from FL_FILEDLG_STUB:
 *   (unset)/ok  print FL_FILEDLG_STUB_PATH or /tmp/fl-filedlg-picked, exit 0
 *   cancel      exit 1
 *   fail        exit 2
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int _dowildcard = 0;

int wmain(int argc, wchar_t **argv);

int wmain(int argc, wchar_t **argv)
{
    const char *mode;
    const char *path;
    (void)argc;
    (void)argv;
    mode = getenv("FL_FILEDLG_STUB");
    if (mode != NULL && strcmp(mode, "cancel") == 0) {
        return 1;
    }
    if (mode != NULL && strcmp(mode, "fail") == 0) {
        return 2;
    }
    path = getenv("FL_FILEDLG_STUB_PATH");
    if (path == NULL || path[0] == '\0') {
        path = "/tmp/fl-filedlg-picked";
    }
    printf("%s\n", path);
    fflush(stdout);
    return 0;
}
