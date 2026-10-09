/*
 * noop.c - B2 spike baseline: the cheapest possible PE (exit 0). Built as
 * noop-con.exe and noop-gui.exe to measure bare Wine process start-up cost.
 */
int _dowildcard = 0;

int wmain(void)
{
    return 0;
}
