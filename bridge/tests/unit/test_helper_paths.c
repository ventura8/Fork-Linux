/*
 * test_helper_paths.c - native unit tests for the path helpers of fl-bridge-helper
 * (bridge/unix/fl_helper_util.c): PATH joining, the Unix -> Windows argument
 * translation of the fl-winexec persona and the WINEDLLOVERRIDES guard.
 *
 * Build (any C11 compiler, ideally with -fsanitize=address,undefined):
 *   cc -std=c11 -Ibridge/common -Ibridge/unix bridge/tests/unit/test_helper_paths.c \
 *      bridge/unix/fl_helper_util.c bridge/common/fl_proto.c bridge/common/fl_sha256.c
 * Exit status 0 when every check passes.
 */
#include <stdio.h>
#include <string.h>

#include "fl_helper_util.h"

static int g_checks;
static int g_failures;

#define CHECK(cond)                                                                        \
    do {                                                                                   \
        g_checks++;                                                                        \
        if (!(cond)) {                                                                     \
            g_failures++;                                                                  \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);       \
        }                                                                                  \
    } while (0)

/* fl_unix_to_win(path, anchors, prefix) must succeed and give want. */
static void xlate_ok(int line, const char *path, const char *anchors, const char *prefix, const char *want)
{
    char out[512];
    int rc = fl_unix_to_win(path, anchors, prefix, out, sizeof(out));
    g_checks++;
    if (rc != 0 || strcmp(out, want) != 0) {
        g_failures++;
        fprintf(stderr, "%s:%d: fl_unix_to_win(\"%s\") = %d \"%s\", want \"%s\"\n", __FILE__, line, path, rc,
                rc == 0 ? out : "", want);
    }
}

#define XLATE(path, anchors, prefix, want) xlate_ok(__LINE__, path, anchors, prefix, want)

static void test_path_join(void)
{
    char out[16];
    CHECK(fl_path_join("/usr/bin", 8, "git", out, sizeof(out)) == 0 && strcmp(out, "/usr/bin/git") == 0);
    CHECK(fl_path_join("/usr/bin/", 9, "git", out, sizeof(out)) == 0 && strcmp(out, "/usr/bin/git") == 0);
    CHECK(fl_path_join("/usr/bin:/bin", 8, "git", out, sizeof(out)) == 0 && strcmp(out, "/usr/bin/git") == 0);
    CHECK(fl_path_join("/", 1, "git", out, sizeof(out)) == 0 && strcmp(out, "/git") == 0);
    CHECK(fl_path_join("/usr/bin", 8, "git123", out, sizeof(out)) == 0);     /* 15 + NUL fits */
    CHECK(fl_path_join("/usr/bin", 8, "git1234", out, sizeof(out)) != 0);    /* one byte over */
    CHECK(fl_path_join("/usr/bin", 8, "x", out, 0) != 0);
}

static void test_unix_to_win(void)
{
    static const char *anchors = "/home/u=Z:\\home\\u;/home/u/repo=C:\\Repo\\;/srv/a=b=D:\\a=b";
    char out[64];

    /* Z: fallback; separators, duplicate and trailing slashes */
    XLATE("/", NULL, NULL, "Z:\\");
    XLATE("/etc/passwd", NULL, NULL, "Z:\\etc\\passwd");
    XLATE("//opt///x//", NULL, NULL, "Z:\\opt\\x");
    XLATE("/a b/\xc3\xbc\xe2\x82\xac", "", "", "Z:\\a b\\\xc3\xbc\xe2\x82\xac");

    /* anchors: longest component prefix wins, trailing separators of the win side dropped */
    XLATE("/home/u/repo/.git/COMMIT_EDITMSG", anchors, NULL, "C:\\Repo\\.git\\COMMIT_EDITMSG");
    XLATE("/home/u/repo", anchors, NULL, "C:\\Repo");
    XLATE("/home/u/repo2/x", anchors, NULL, "Z:\\home\\u\\repo2\\x");
    XLATE("/home/u", anchors, NULL, "Z:\\home\\u");
    XLATE("/home/user", anchors, NULL, "Z:\\home\\user");
    /* an '=' inside the unix side: split where the Windows path starts */
    XLATE("/srv/a=b/f", anchors, NULL, "D:\\a=b\\f");
    /* forward slashes on the win side, a trailing '/' on the unix side, a root anchor */
    XLATE("/data/x/y", "/data/=E:/stuff/", NULL, "E:\\stuff\\x\\y");
    XLATE("/data", "/data/=E:/", NULL, "E:\\");
    XLATE("/anything/at/all", "/=Y:\\", NULL, "Y:\\anything\\at\\all");
    XLATE("/net/share/f", "/net/share=\\\\server\\share", NULL, "\\\\server\\share\\f");
    /* malformed entries are ignored */
    XLATE("/home/u/f", "relative=C:\\x;/home/u=;=C:\\y;;/nohit", NULL, "Z:\\home\\u\\f");

    /* WINEPREFIX/drive_c is an implicit C: anchor in the longest match */
    XLATE("/p/pfx/drive_c/users/u/a.txt", NULL, "/p/pfx", "C:\\users\\u\\a.txt");
    XLATE("/p/pfx/drive_c", NULL, "/p/pfx/", "C:\\");
    XLATE("/p/pfx/drive_cx/a", NULL, "/p/pfx", "Z:\\p\\pfx\\drive_cx\\a");
    XLATE("/p/pfx/dosdevices/a", NULL, "/p/pfx", "Z:\\p\\pfx\\dosdevices\\a");
    XLATE("/p/pfx/drive_c/a", "/p=Z:\\p", "/p/pfx", "C:\\a");
    XLATE("/p/pfx/drive_c/repo/f", "/p/pfx/drive_c/repo=R:\\", "/p/pfx", "R:\\f");
    XLATE("/p/pfx/drive_c/a", "/p/pfx/drive_c=K:\\", "/p/pfx", "K:\\a");   /* explicit anchor wins a tie */
    XLATE("/x/drive_c/a", NULL, "relative", "Z:\\x\\drive_c\\a");
    XLATE("/drive_c/a", NULL, "/", "Z:\\drive_c\\a");

    /* errors */
    CHECK(fl_unix_to_win("relative/path", NULL, NULL, out, sizeof(out)) != 0);
    CHECK(fl_unix_to_win("", NULL, NULL, out, sizeof(out)) != 0);
    CHECK(fl_unix_to_win(NULL, NULL, NULL, out, sizeof(out)) != 0);
    CHECK(fl_unix_to_win("/a", NULL, NULL, out, 0) != 0);
    CHECK(fl_unix_to_win("/abc", NULL, NULL, out, 7) == 0 && strcmp(out, "Z:\\abc") == 0);
    CHECK(fl_unix_to_win("/abcd", NULL, NULL, out, 7) != 0);
}

static void test_dlloverrides(void)
{
    char out[64];
    CHECK(fl_dlloverrides(NULL, out, sizeof(out)) == 0 && strcmp(out, "winemenubuilder.exe=d") == 0);
    CHECK(fl_dlloverrides("", out, sizeof(out)) == 0 && strcmp(out, "winemenubuilder.exe=d") == 0);
    CHECK(fl_dlloverrides("mscoree,mshtml=", out, sizeof(out)) == 0 &&
          strcmp(out, "mscoree,mshtml=;winemenubuilder.exe=d") == 0);
    CHECK(fl_dlloverrides("WineMenuBuilder.exe=", out, sizeof(out)) == 0 && strcmp(out, "WineMenuBuilder.exe=") == 0);
    CHECK(fl_dlloverrides("a=n;winemenubuilder=d", out, sizeof(out)) == 0 && strcmp(out, "a=n;winemenubuilder=d") == 0);
    CHECK(fl_dlloverrides("winemenubuilde", out, sizeof(out)) == 0 &&
          strcmp(out, "winemenubuilde;winemenubuilder.exe=d") == 0);
    CHECK(fl_dlloverrides(NULL, out, 21) != 0);
    CHECK(fl_dlloverrides(NULL, out, 22) == 0);
    CHECK(fl_dlloverrides("x", out, 23) != 0);
    CHECK(fl_dlloverrides("x", out, 24) == 0);
    CHECK(fl_dlloverrides("winemenubuilder", out, 15) != 0);
}

int main(void)
{
    test_path_join();
    test_unix_to_win();
    test_dlloverrides();
    if (g_failures) {
        fprintf(stderr, "test_helper_paths: %d of %d checks failed\n", g_failures, g_checks);
        return 1;
    }
    printf("test_helper_paths: %d checks passed\n", g_checks);
    return 0;
}
