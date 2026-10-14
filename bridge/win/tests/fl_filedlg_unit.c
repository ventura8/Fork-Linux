/*
 * fl_filedlg_unit.c - TEST-ONLY unit tests of bridge/win/fl_filedlg.c under Wine
 * (tests/bridge/test_shims_wine_coverage.py). Compiles fl_filedlg.c in-process
 * (like fl_win_unit.c) so --wrap=malloc can hit OOM paths; never installed.
 *
 *   fl-filedlg-unit.exe <dir-with-stub-fl-launch.exe> [picked-unix-path]
 *
 * <dir> must contain fl-launch.exe (the stub). DllMain is primed so Show() looks
 * for that stub next to this executable's directory copy layout: the test copies
 * this exe and the stub into <dir> before running.
 */
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <windows.h>
#include <objbase.h>
#include <shobjidl.h>
#include <shlobj.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* fl_filedlg.c exports (linked in; it also defines _dowildcard). */
BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved);
HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void **obj);
HRESULT WINAPI DllCanUnloadNow(void);
void fl_filedlg_unit_set_module(HMODULE module);
void fl_filedlg_unit_force_wine(int enable);

static const GUID CLSID_FileOpenDialog_fl =
    { 0xDC1C5A9C, 0xE88A, 0x4dde, { 0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7 } };
static const GUID CLSID_FileSaveDialog_fl =
    { 0xC0B4E2F3, 0xBA21, 0x4773, { 0x8D, 0xBA, 0x33, 0x5E, 0xC9, 0x46, 0xEB, 0x8B } };

/* ---- checks -------------------------------------------------------------- */

static int g_checks;
static int g_failed;

static void check_at(int ok, int line, const char *what)
{
    g_checks++;
    if (!ok) {
        g_failed++;
        printf("FAIL line %d: %s\n", line, what);
    }
}

#define CHECK(c) check_at((c) ? 1 : 0, __LINE__, #c)

static HRESULT make_folder_item(const WCHAR *win_path, IShellItem **item)
{
    return SHCreateItemFromParsingName(win_path, NULL, &IID_IShellItem, (void **)item);
}

static IFileDialog *new_dialog(int save)
{
    IClassFactory *factory = NULL;
    IFileDialog *dlg = NULL;
    const GUID *clsid = save ? &CLSID_FileSaveDialog_fl : &CLSID_FileOpenDialog_fl;
    const IID *iid = save ? &IID_IFileSaveDialog : &IID_IFileOpenDialog;
    if (FAILED(DllGetClassObject(clsid, &IID_IClassFactory, (void **)&factory)) || factory == NULL) {
        return NULL;
    }
    if (FAILED(factory->lpVtbl->CreateInstance(factory, NULL, iid, (void **)&dlg))) {
        dlg = NULL;
    }
    factory->lpVtbl->Release(factory);
    return dlg;
}

static void exercise_setters(IFileDialog *dlg)
{
    FILEOPENDIALOGOPTIONS opts = 0;
    DWORD cookie = 0;
    UINT index = 0;
    LPWSTR name = NULL;
    IShellItem *result = NULL;

    CHECK(SUCCEEDED(dlg->lpVtbl->SetTitle(dlg, L"unit")));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetOkButtonLabel(dlg, L"OK")));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileNameLabel(dlg, L"File")));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileTypes(dlg, 0, NULL)));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileTypeIndex(dlg, 1)));
    CHECK(SUCCEEDED(dlg->lpVtbl->GetFileTypeIndex(dlg, &index)));
    CHECK(index == 1);
    CHECK(dlg->lpVtbl->GetFileTypeIndex(dlg, NULL) == E_POINTER);
    CHECK(SUCCEEDED(dlg->lpVtbl->Advise(dlg, NULL, &cookie)));
    CHECK(dlg->lpVtbl->Advise(dlg, NULL, NULL) == E_POINTER);
    CHECK(SUCCEEDED(dlg->lpVtbl->Unadvise(dlg, cookie)));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetOptions(dlg, FOS_PATHMUSTEXIST | FOS_FILEMUSTEXIST)));
    CHECK(SUCCEEDED(dlg->lpVtbl->GetOptions(dlg, &opts)));
    CHECK(dlg->lpVtbl->GetOptions(dlg, NULL) == E_POINTER);
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileName(dlg, L"out.txt")));
    CHECK(SUCCEEDED(dlg->lpVtbl->GetFileName(dlg, &name)));
    CoTaskMemFree(name);
    CHECK(dlg->lpVtbl->GetFileName(dlg, NULL) == E_POINTER);
    CHECK(SUCCEEDED(dlg->lpVtbl->SetDefaultExtension(dlg, L"txt")));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetClientGuid(dlg, &CLSID_FileOpenDialog_fl)));
    CHECK(SUCCEEDED(dlg->lpVtbl->ClearClientData(dlg)));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFilter(dlg, NULL)));
    CHECK(SUCCEEDED(dlg->lpVtbl->AddPlace(dlg, NULL, FDAP_TOP)));
    CHECK(SUCCEEDED(dlg->lpVtbl->Close(dlg, S_OK)));
    CHECK(dlg->lpVtbl->GetResult(dlg, NULL) == E_POINTER);
    CHECK(dlg->lpVtbl->GetResult(dlg, &result) == E_UNEXPECTED);
    CHECK(dlg->lpVtbl->QueryInterface(dlg, &IID_IFileDialog, NULL) == E_POINTER);
    CHECK(dlg->lpVtbl->QueryInterface(dlg, &IID_IClassFactory, (void **)&result) == E_NOINTERFACE);
    CHECK(dlg->lpVtbl->SetFolder(dlg, NULL) == E_INVALIDARG);
    CHECK(dlg->lpVtbl->SetDefaultFolder(dlg, NULL) == E_INVALIDARG);
    CHECK(dlg->lpVtbl->GetFolder(dlg, NULL) == E_POINTER);
    CHECK(dlg->lpVtbl->GetFolder(dlg, &result) == E_NOTIMPL);
    CHECK(result == NULL);
    CHECK(dlg->lpVtbl->GetCurrentSelection(dlg, &result) == E_UNEXPECTED);
    CHECK(SUCCEEDED(dlg->lpVtbl->SetTitle(dlg, NULL)));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileName(dlg, NULL)));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetDefaultExtension(dlg, NULL)));
}

static void exercise_show(IFileDialog *dlg, int is_save, const WCHAR *folder_win)
{
    IShellItem *folder = NULL;
    IShellItem *result = NULL;
    IFileOpenDialog *open = NULL;
    IFileSaveDialog *save = NULL;
    IShellItemArray *items = NULL;
    IPropertyStore *props = NULL;
    HRESULT hr;

    if (SUCCEEDED(make_folder_item(folder_win, &folder))) {
        CHECK(SUCCEEDED(dlg->lpVtbl->SetDefaultFolder(dlg, folder)));
        CHECK(SUCCEEDED(dlg->lpVtbl->SetFolder(dlg, folder)));
        folder->lpVtbl->Release(folder);
    }

    if (is_save) {
        CHECK(SUCCEEDED(dlg->lpVtbl->QueryInterface(dlg, &IID_IFileSaveDialog, (void **)&save)));
        CHECK(SUCCEEDED(save->lpVtbl->SetProperties(save, NULL)));
        CHECK(SUCCEEDED(save->lpVtbl->SetCollectedProperties(save, NULL, FALSE)));
        CHECK(save->lpVtbl->GetProperties(save, NULL) == E_POINTER);
        CHECK(save->lpVtbl->GetProperties(save, &props) == E_NOTIMPL);
        CHECK(SUCCEEDED(save->lpVtbl->ApplyProperties(save, NULL, NULL, NULL, NULL)));
        if (SUCCEEDED(make_folder_item(folder_win, &folder))) {
            CHECK(SUCCEEDED(save->lpVtbl->SetSaveAsItem(save, folder)));
            folder->lpVtbl->Release(folder);
        }
        save->lpVtbl->Release(save);
    } else {
        CHECK(SUCCEEDED(dlg->lpVtbl->QueryInterface(dlg, &IID_IFileOpenDialog, (void **)&open)));
        CHECK(open->lpVtbl->GetResults(open, NULL) == E_POINTER);
        CHECK(FAILED(open->lpVtbl->GetResults(open, &items)));
        open->lpVtbl->Release(open);
    }

    CHECK(SUCCEEDED(dlg->lpVtbl->SetOptions(dlg, FOS_PICKFOLDERS)));
    hr = dlg->lpVtbl->Show(dlg, NULL);
    CHECK(SUCCEEDED(hr));
    CHECK(SUCCEEDED(dlg->lpVtbl->GetResult(dlg, &result)));
    if (result != NULL) {
        CHECK(SUCCEEDED(dlg->lpVtbl->GetCurrentSelection(dlg, &folder)));
        if (folder != NULL) {
            folder->lpVtbl->Release(folder);
        }
        result->lpVtbl->Release(result);
    }
    if (!is_save) {
        CHECK(SUCCEEDED(dlg->lpVtbl->QueryInterface(dlg, &IID_IFileOpenDialog, (void **)&open)));
        CHECK(SUCCEEDED(open->lpVtbl->GetResults(open, &items)));
        if (items != NULL) {
            items->lpVtbl->Release(items);
        }
        CHECK(SUCCEEDED(open->lpVtbl->GetSelectedItems(open, &items)));
        if (items != NULL) {
            items->lpVtbl->Release(items);
        }
        open->lpVtbl->Release(open);
    }

    CHECK(SUCCEEDED(dlg->lpVtbl->SetOptions(dlg, is_save ? (FOS_PATHMUSTEXIST | FOS_OVERWRITEPROMPT)
                                                          : (FOS_PATHMUSTEXIST | FOS_FILEMUSTEXIST))));
    CHECK(SUCCEEDED(dlg->lpVtbl->SetFileName(dlg, L"save-me.txt")));
    CHECK(SUCCEEDED(dlg->lpVtbl->Show(dlg, NULL)));
}

static void exercise_factory_and_oom(void)
{
    IClassFactory *factory = NULL;
    IFileDialog *dlg = NULL;
    IUnknown *unk = NULL;
    GUID bogus = { 0 };

    CHECK(DllGetClassObject(&CLSID_FileOpenDialog_fl, &IID_IClassFactory, NULL) == E_POINTER);
    CHECK(DllGetClassObject(&bogus, &IID_IClassFactory, (void **)&factory) == CLASS_E_CLASSNOTAVAILABLE);

    CHECK(SUCCEEDED(DllGetClassObject(&CLSID_FileOpenDialog_fl, &IID_IClassFactory, (void **)&factory)));
    if (factory != NULL) {
        CHECK(factory->lpVtbl->QueryInterface(factory, &IID_IClassFactory, NULL) == E_POINTER);
        CHECK(factory->lpVtbl->CreateInstance(factory, (IUnknown *)factory, &IID_IFileDialog, (void **)&dlg) ==
              CLASS_E_NOAGGREGATION);
        CHECK(SUCCEEDED(factory->lpVtbl->LockServer(factory, TRUE)));
        CHECK(DllCanUnloadNow() == S_FALSE);
        CHECK(SUCCEEDED(factory->lpVtbl->LockServer(factory, FALSE)));
        CHECK(SUCCEEDED(factory->lpVtbl->QueryInterface(factory, &IID_IUnknown, (void **)&unk)));
        if (unk != NULL) {
            unk->lpVtbl->Release(unk);
        }
        CHECK(factory->lpVtbl->QueryInterface(factory, &IID_IFileDialog, (void **)&unk) == E_NOINTERFACE);
        factory->lpVtbl->Release(factory);
    }
}

static void exercise_cancel_and_fail(const WCHAR *folder_win)
{
    IFileDialog *dlg;
    HMODULE saved;

    SetEnvironmentVariableA("FL_FILEDLG_STUB", "cancel");
    dlg = new_dialog(0);
    if (dlg != NULL) {
        CHECK(FAILED(dlg->lpVtbl->Show(dlg, NULL)));
        dlg->lpVtbl->Release(dlg);
    }
    SetEnvironmentVariableA("FL_FILEDLG_STUB", "fail");
    dlg = new_dialog(0);
    if (dlg != NULL) {
        CHECK(FAILED(dlg->lpVtbl->Show(dlg, NULL)));
        dlg->lpVtbl->Release(dlg);
    }
    SetEnvironmentVariableA("FL_FILEDLG_STUB", NULL);

    /* Missing fl-launch.exe → CreateProcessW fails inside Show(). */
    {
        WCHAR mod[MAX_PATH];
        WCHAR launch[MAX_PATH];
        WCHAR renamed[MAX_PATH];
        DWORD n = GetModuleFileNameW(NULL, mod, MAX_PATH);
        WCHAR *slash;
        if (n > 0 && n < MAX_PATH) {
            slash = wcsrchr(mod, L'\\');
            if (slash != NULL) {
                slash[1] = L'\0';
                _snwprintf(launch, MAX_PATH, L"%sfl-launch.exe", mod);
                _snwprintf(renamed, MAX_PATH, L"%sfl-launch.bak", mod);
                if (MoveFileW(launch, renamed)) {
                    dlg = new_dialog(0);
                    if (dlg != NULL) {
                        CHECK(FAILED(dlg->lpVtbl->Show(dlg, NULL)));
                        dlg->lpVtbl->Release(dlg);
                    }
                    MoveFileW(renamed, launch);
                }
            }
        }
    }

    /* fl_launch_path: GetModuleFileNameW fails on a bogus HMODULE. */
    saved = GetModuleHandleW(NULL);
    fl_filedlg_unit_set_module((HMODULE)(uintptr_t)1);
    dlg = new_dialog(0);
    if (dlg != NULL) {
        CHECK(FAILED(dlg->lpVtbl->Show(dlg, NULL)));
        dlg->lpVtbl->Release(dlg);
    }
    fl_filedlg_unit_set_module(saved);

    /* Path conversion without Wine helpers (copy / UTF-16 fallbacks). */
    fl_filedlg_unit_force_wine(0);
    dlg = new_dialog(0);
    if (dlg != NULL) {
        IShellItem *folder = NULL;
        if (SUCCEEDED(make_folder_item(folder_win, &folder))) {
            CHECK(SUCCEEDED(dlg->lpVtbl->SetFolder(dlg, folder)));
            folder->lpVtbl->Release(folder);
        }
        CHECK(SUCCEEDED(dlg->lpVtbl->SetOptions(dlg, FOS_PICKFOLDERS)));
        CHECK(SUCCEEDED(dlg->lpVtbl->Show(dlg, NULL)));
        dlg->lpVtbl->Release(dlg);
    }
    /* Invalid UTF-8 from the stub → fl_utf16 / fl_set_result failure. */
    SetEnvironmentVariableA("FL_FILEDLG_STUB_PATH", "\xff\xfe\xff");
    dlg = new_dialog(0);
    if (dlg != NULL) {
        CHECK(FAILED(dlg->lpVtbl->Show(dlg, NULL)));
        dlg->lpVtbl->Release(dlg);
    }
    fl_filedlg_unit_force_wine(1);
}

static int run_suite(const WCHAR *folder_win)
{
    IFileDialog *dlg;

    exercise_factory_and_oom();

    dlg = new_dialog(0);
    CHECK(dlg != NULL);
    if (dlg != NULL) {
        exercise_setters(dlg);
        exercise_show(dlg, 0, folder_win);
        dlg->lpVtbl->Release(dlg);
    }

    dlg = new_dialog(1);
    CHECK(dlg != NULL);
    if (dlg != NULL) {
        exercise_setters(dlg);
        exercise_show(dlg, 1, folder_win);
        dlg->lpVtbl->Release(dlg);
    }

    exercise_cancel_and_fail(folder_win);
    CHECK(DllCanUnloadNow() == S_OK);
    return g_failed;
}

int wmain(int argc, wchar_t **argv);

int wmain(int argc, wchar_t **argv)
{
    WCHAR folder_win[MAX_PATH];
    char picked[512];
    HMODULE self;
    int rc;

    if (argc < 2) {
        fprintf(stderr, "usage: fl-filedlg-unit.exe <dir> [picked-unix-path]\n");
        return 2;
    }
    if (argc >= 3) {
        if (WideCharToMultiByte(CP_UTF8, 0, argv[2], -1, picked, (int)sizeof(picked), NULL, NULL) <= 0) {
            fprintf(stderr, "bad picked path\n");
            return 2;
        }
    } else {
        snprintf(picked, sizeof(picked), "/tmp/fl-filedlg-picked");
    }
    SetEnvironmentVariableA("FL_FILEDLG_STUB_PATH", picked);

    if (GetFullPathNameW(argv[1], MAX_PATH, folder_win, NULL) == 0) {
        wcsncpy(folder_win, argv[1], MAX_PATH - 1);
        folder_win[MAX_PATH - 1] = L'\0';
    }

    self = GetModuleHandleW(NULL);
    DllMain(self, DLL_PROCESS_ATTACH, NULL);

    CoInitializeEx(NULL, COINIT_APARTMENTTHREADED);
    rc = run_suite(folder_win);
    CoUninitialize();
    printf("fl-filedlg-unit: %d checks, %d failed\n", g_checks, g_failed);
    return rc == 0 ? 0 : 1;
}
