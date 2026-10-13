/*
 * fl_gcov_exit.c - coverage builds only: flush the gcov counters of a PE shim before
 * ExitProcess.
 *
 * Linked into fl-shim.exe / fl-launch.exe only when meson is configured with
 * -Db_coverage=true (scripts/ci-c-coverage.sh); release builds never contain it.
 *
 * libgcov writes its .gcda files from an atexit handler. The shims leave through
 * flw_exit() -> ExitProcess(), which ends the process without running the C runtime's
 * atexit table (and Wine does not deliver DLL_PROCESS_DETACH to an EXE's TLS callbacks),
 * so a coverage run would record nothing. A constructor swaps the EXE's import slot of
 * kernel32!ExitProcess for a wrapper that calls __gcov_dump() first. The source of the
 * shims stays untouched.
 */
#include <windows.h>

void __gcov_dump(void); /* libgcov (--coverage) */

/* The import address table slot the MinGW toolchain emits for kernel32!ExitProcess. */
extern void(WINAPI *__imp_ExitProcess)(UINT);

static void(WINAPI *fl_real_exit)(UINT);

static void WINAPI fl_gcov_exit(UINT code)
{
    __gcov_dump();
    fl_real_exit(code);
}

__attribute__((constructor)) static void fl_gcov_hook(void)
{
    DWORD old = 0;
    fl_real_exit = __imp_ExitProcess;
    if (VirtualProtect((LPVOID)&__imp_ExitProcess, sizeof(void *), PAGE_READWRITE, &old)) {
        __imp_ExitProcess = fl_gcov_exit;
        (void)VirtualProtect((LPVOID)&__imp_ExitProcess, sizeof(void *), old, &old);
    }
}
