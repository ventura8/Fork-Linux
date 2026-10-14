/*
 * fl_filedlg.c - In-process COM server that replaces Wine's FileOpenDialog /
 * FileSaveDialog for Fork under Wine: Show() runs fl-launch.exe pick-* through
 * the bridge daemon, so the user gets zenity/kdialog instead of Wine's dialog.
 *
 * Registered as InprocServer32 for CLSID_FileOpenDialog and CLSID_FileSaveDialog
 * when [integration] system_file_dialogs is on (host_integration setup step).
 *
 * Never calls flw_exit / ExitProcess: this DLL is loaded into Fork.exe.
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

/* MinGW: never glob. */
int _dowildcard = 0;

static const GUID CLSID_FileOpenDialog_fl =
    { 0xDC1C5A9C, 0xE88A, 0x4dde, { 0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7 } };
static const GUID CLSID_FileSaveDialog_fl =
    { 0xC0B4E2F3, 0xBA21, 0x4773, { 0x8D, 0xBA, 0x33, 0x5E, 0xC9, 0x46, 0xEB, 0x8B } };

enum { FL_KIND_OPEN = 0, FL_KIND_SAVE = 1 };
enum { FL_PATH_MAX = 32768 };

typedef WCHAR *(*wine_dos_fn)(const char *);
typedef char *(*wine_unix_fn)(const WCHAR *);

struct fl_dlg {
    IFileDialogVtbl *vtbl;
    LONG refs;
    int kind; /* FL_KIND_OPEN or FL_KIND_SAVE */
    DWORD options;
    WCHAR *title;
    WCHAR *folder;
    WCHAR *filename;
    WCHAR *defext;
    IShellItem *result;
};

struct fl_factory {
    IClassFactoryVtbl *vtbl;
    LONG refs;
    int kind;
};

static LONG g_locks;
static HMODULE g_module;

#ifdef FL_FILEDLG_UNIT
/* Test-only hooks (fl-filedlg-unit.exe); never defined in the shipped DLL. */
void fl_filedlg_unit_set_module(HMODULE module);
void fl_filedlg_unit_force_wine(int enable);

static int g_force_no_wine;

void fl_filedlg_unit_set_module(HMODULE module)
{
    g_module = module;
}

void fl_filedlg_unit_force_wine(int enable)
{
    g_force_no_wine = enable ? 0 : 1;
}
#endif

/* ---- small helpers ------------------------------------------------------ */

static WCHAR *fl_wcsdup(const WCHAR *s)
{
    size_t n;
    WCHAR *out;
    if (s == NULL) {
        return NULL;
    }
    n = wcslen(s) + 1u;
    out = (WCHAR *)CoTaskMemAlloc(n * sizeof(WCHAR));
    if (out != NULL) {
        memcpy(out, s, n * sizeof(WCHAR));
    }
    return out;
}

static void fl_free_w(WCHAR **p)
{
    if (p != NULL && *p != NULL) {
        CoTaskMemFree(*p);
        *p = NULL;
    }
}

static WCHAR *fl_utf16(const char *s)
{
    int n;
    WCHAR *w;
    if (s == NULL) {
        return NULL;
    }
    n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, -1, NULL, 0);
    if (n <= 0) {
        return NULL;
    }
    w = (WCHAR *)malloc((size_t)n * sizeof(WCHAR));
    if (w == NULL) {
        return NULL;
    }
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s, -1, w, n) <= 0) {
        free(w);
        return NULL;
    }
    return w;
}

static wine_dos_fn fl_wine_dos(void)
{
    static wine_dos_fn fn;
    static int looked;
#ifdef FL_FILEDLG_UNIT
    if (g_force_no_wine) {
        return NULL;
    }
#endif
    if (!looked) {
        looked = 1;
        fn = (wine_dos_fn)(void (*)(void))GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "wine_get_dos_file_name");
    }
    return fn;
}

static wine_unix_fn fl_wine_unix(void)
{
    static wine_unix_fn fn;
    static int looked;
#ifdef FL_FILEDLG_UNIT
    if (g_force_no_wine) {
        return NULL;
    }
#endif
    if (!looked) {
        looked = 1;
        fn = (wine_unix_fn)(void (*)(void))GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "wine_get_unix_file_name");
    }
    return fn;
}

/* Windows path → malloc'd UTF-16 of the Unix path for fl-launch (zenity wants Linux). */
static WCHAR *fl_win_to_unix_arg(const WCHAR *win)
{
    wine_unix_fn unix_fn = fl_wine_unix();
    char *unix_path;
    WCHAR *out;
    size_t n;
    if (win == NULL || win[0] == L'\0') {
        return NULL;
    }
    if (unix_fn != NULL) {
        unix_path = unix_fn(win);
        if (unix_path != NULL) {
            out = fl_utf16(unix_path);
            HeapFree(GetProcessHeap(), 0, unix_path);
            return out;
        }
    }
    n = wcslen(win) + 1u;
    out = (WCHAR *)malloc(n * sizeof(WCHAR));
    if (out != NULL) {
        memcpy(out, win, n * sizeof(WCHAR));
    }
    return out;
}

static WCHAR *fl_unix_to_win(const char *unix_path)
{
    wine_dos_fn dos = fl_wine_dos();
    WCHAR *win;
    if (dos == NULL || unix_path == NULL) {
        return fl_utf16(unix_path);
    }
    win = dos(unix_path);
    if (win == NULL) {
        return fl_utf16(unix_path);
    }
    return win; /* HeapAlloc from Wine; free with HeapFree */
}

static void fl_free_win_path(WCHAR *win)
{
    if (win == NULL) {
        return;
    }
    if (fl_wine_dos() != NULL) {
        HeapFree(GetProcessHeap(), 0, win);
    } else {
        free(win);
    }
}

static int fl_launch_path(WCHAR *out, size_t out_wchars)
{
    WCHAR mod[MAX_PATH];
    WCHAR *slash;
    DWORD n = GetModuleFileNameW(g_module, mod, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        return -1;
    }
    slash = wcsrchr(mod, L'\\');
    if (slash == NULL) {
        slash = wcsrchr(mod, L'/');
    }
    if (slash == NULL) {
        return -1;
    }
    slash[1] = L'\0';
    if (wcslen(mod) + wcslen(L"fl-launch.exe") + 1u >= out_wchars) {
        return -1;
    }
    wcscpy(out, mod);
    wcscat(out, L"fl-launch.exe");
    return 0;
}

static int fl_pick_cmdline(WCHAR *cmdline, size_t cap, const WCHAR *launch, const WCHAR *verb,
                           const WCHAR *start, const WCHAR *name)
{
    int ncmd;
    if (name != NULL && name[0] != L'\0' && wcscmp(verb, L"save-file") == 0) {
        ncmd = _snwprintf(cmdline, cap, L"\"%s\" %s \"%s\" \"%s\"", launch, verb,
                          start != NULL && start[0] != L'\0' ? start : L".", name);
    } else if (start != NULL && start[0] != L'\0') {
        ncmd = _snwprintf(cmdline, cap, L"\"%s\" %s \"%s\"", launch, verb, start);
    } else {
        ncmd = _snwprintf(cmdline, cap, L"\"%s\" %s", launch, verb);
    }
    return (ncmd < 0 || (size_t)ncmd >= cap) ? -1 : 0;
}

static size_t fl_read_stdout(HANDLE rd, char *out, size_t outsz)
{
    size_t got = 0;
    char buf[4096];
    while (got + 1u < outsz) {
        DWORD n = 0;
        size_t room = outsz - 1u - got;
        if (!ReadFile(rd, buf, sizeof(buf), &n, NULL) || n == 0) {
            break;
        }
        if ((size_t)n > room) {
            n = (DWORD)room;
        }
        memcpy(out + got, buf, n);
        got += n;
    }
    out[got] = '\0';
    return got;
}

static void fl_trim_first_line(char *out)
{
    char *nl = strchr(out, '\n');
    if (nl != NULL) {
        *nl = '\0';
    }
    if (out[0] != '\0' && out[strlen(out) - 1u] == '\r') {
        out[strlen(out) - 1u] = '\0';
    }
}

/* Run fl-launch.exe pick-* ; write the first stdout line (UTF-8) into out. */
static HRESULT fl_run_pick(const WCHAR *verb, const WCHAR *start, const WCHAR *name, char *out, size_t outsz)
{
    WCHAR launch[MAX_PATH];
    WCHAR cmdline[FL_PATH_MAX];
    SECURITY_ATTRIBUTES sa;
    HANDLE rd = NULL;
    HANDLE wr = NULL;
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    DWORD code = 1;

    if (fl_launch_path(launch, MAX_PATH) != 0) {
        return E_FAIL;
    }
    if (fl_pick_cmdline(cmdline, FL_PATH_MAX, launch, verb, start, name) != 0) {
        return E_FAIL;
    }

    memset(&sa, 0, sizeof(sa));
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;
    if (!CreatePipe(&rd, &wr, &sa, 0)) {
        return E_FAIL;
    }
    SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = wr;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessW(launch, cmdline, NULL, NULL, TRUE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) {
        CloseHandle(rd);
        CloseHandle(wr);
        return E_FAIL;
    }
    CloseHandle(wr);
    CloseHandle(pi.hThread);
    (void)fl_read_stdout(rd, out, outsz);
    CloseHandle(rd);
    WaitForSingleObject(pi.hProcess, INFINITE);
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    if (code == 1) {
        return HRESULT_FROM_WIN32(ERROR_CANCELLED);
    }
    if (code != 0) {
        return E_FAIL;
    }
    fl_trim_first_line(out);
    return out[0] != '\0' ? S_OK : HRESULT_FROM_WIN32(ERROR_CANCELLED);
}

static HRESULT fl_set_result(struct fl_dlg *d, const char *unix_path)
{
    WCHAR *win;
    IShellItem *item = NULL;
    HRESULT hr;
    win = fl_unix_to_win(unix_path);
    if (win == NULL) {
        return E_OUTOFMEMORY;
    }
    hr = SHCreateItemFromParsingName(win, NULL, &IID_IShellItem, (void **)&item);
    fl_free_win_path(win);
    if (FAILED(hr)) {
        return hr;
    }
    if (d->result != NULL) {
        d->result->lpVtbl->Release(d->result);
    }
    d->result = item;
    return S_OK;
}

/* ---- IFileDialog -------------------------------------------------------- */

static HRESULT STDMETHODCALLTYPE fl_QueryInterface(IFileDialog *iface, REFIID riid, void **obj);
static ULONG STDMETHODCALLTYPE fl_AddRef(IFileDialog *iface);
static ULONG STDMETHODCALLTYPE fl_Release(IFileDialog *iface);
static HRESULT STDMETHODCALLTYPE fl_Show(IFileDialog *iface, HWND owner);
static HRESULT STDMETHODCALLTYPE fl_SetFileTypes(IFileDialog *iface, UINT c, const COMDLG_FILTERSPEC *f);
static HRESULT STDMETHODCALLTYPE fl_SetFileTypeIndex(IFileDialog *iface, UINT i);
static HRESULT STDMETHODCALLTYPE fl_GetFileTypeIndex(IFileDialog *iface, UINT *i);
static HRESULT STDMETHODCALLTYPE fl_Advise(IFileDialog *iface, IFileDialogEvents *e, DWORD *cookie);
static HRESULT STDMETHODCALLTYPE fl_Unadvise(IFileDialog *iface, DWORD cookie);
static HRESULT STDMETHODCALLTYPE fl_SetOptions(IFileDialog *iface, FILEOPENDIALOGOPTIONS fos);
static HRESULT STDMETHODCALLTYPE fl_GetOptions(IFileDialog *iface, FILEOPENDIALOGOPTIONS *fos);
static HRESULT STDMETHODCALLTYPE fl_SetDefaultFolder(IFileDialog *iface, IShellItem *item);
static HRESULT STDMETHODCALLTYPE fl_SetFolder(IFileDialog *iface, IShellItem *item);
static HRESULT STDMETHODCALLTYPE fl_GetFolder(IFileDialog *iface, IShellItem **item);
static HRESULT STDMETHODCALLTYPE fl_GetCurrentSelection(IFileDialog *iface, IShellItem **item);
static HRESULT STDMETHODCALLTYPE fl_SetFileName(IFileDialog *iface, LPCWSTR name);
static HRESULT STDMETHODCALLTYPE fl_GetFileName(IFileDialog *iface, LPWSTR *name);
static HRESULT STDMETHODCALLTYPE fl_SetTitle(IFileDialog *iface, LPCWSTR title);
static HRESULT STDMETHODCALLTYPE fl_SetOkButtonLabel(IFileDialog *iface, LPCWSTR label);
static HRESULT STDMETHODCALLTYPE fl_SetFileNameLabel(IFileDialog *iface, LPCWSTR label);
static HRESULT STDMETHODCALLTYPE fl_GetResult(IFileDialog *iface, IShellItem **item);
static HRESULT STDMETHODCALLTYPE fl_AddPlace(IFileDialog *iface, IShellItem *item, FDAP fdap);
static HRESULT STDMETHODCALLTYPE fl_SetDefaultExtension(IFileDialog *iface, LPCWSTR ext);
static HRESULT STDMETHODCALLTYPE fl_Close(IFileDialog *iface, HRESULT hr);
static HRESULT STDMETHODCALLTYPE fl_SetClientGuid(IFileDialog *iface, REFGUID guid);
static HRESULT STDMETHODCALLTYPE fl_ClearClientData(IFileDialog *iface);
static HRESULT STDMETHODCALLTYPE fl_SetFilter(IFileDialog *iface, IShellItemFilter *filter);

/* IFileOpenDialog extras */
static HRESULT STDMETHODCALLTYPE fl_GetResults(IFileOpenDialog *iface, IShellItemArray **items);
static HRESULT STDMETHODCALLTYPE fl_GetSelectedItems(IFileOpenDialog *iface, IShellItemArray **items);

/* IFileSaveDialog extras */
static HRESULT STDMETHODCALLTYPE fl_SetSaveAsItem(IFileSaveDialog *iface, IShellItem *item);
static HRESULT STDMETHODCALLTYPE fl_SetProperties(IFileSaveDialog *iface, IPropertyStore *store);
static HRESULT STDMETHODCALLTYPE fl_SetCollectedProperties(IFileSaveDialog *iface, IPropertyDescriptionList *list, BOOL append);
static HRESULT STDMETHODCALLTYPE fl_GetProperties(IFileSaveDialog *iface, IPropertyStore **store);
static HRESULT STDMETHODCALLTYPE fl_ApplyProperties(IFileSaveDialog *iface, IShellItem *item, IPropertyStore *store, HWND hwnd, IFileOperationProgressSink *sink);

static IFileOpenDialogVtbl g_open_vtbl;
static IFileSaveDialogVtbl g_save_vtbl;
static int g_vtbl_ready;

static void fl_fill_dialog_vtbl(IFileDialogVtbl *base)
{
    base->QueryInterface = fl_QueryInterface;
    base->AddRef = fl_AddRef;
    base->Release = fl_Release;
    base->Show = fl_Show;
    base->SetFileTypes = fl_SetFileTypes;
    base->SetFileTypeIndex = fl_SetFileTypeIndex;
    base->GetFileTypeIndex = fl_GetFileTypeIndex;
    base->Advise = fl_Advise;
    base->Unadvise = fl_Unadvise;
    base->SetOptions = fl_SetOptions;
    base->GetOptions = fl_GetOptions;
    base->SetDefaultFolder = fl_SetDefaultFolder;
    base->SetFolder = fl_SetFolder;
    base->GetFolder = fl_GetFolder;
    base->GetCurrentSelection = fl_GetCurrentSelection;
    base->SetFileName = fl_SetFileName;
    base->GetFileName = fl_GetFileName;
    base->SetTitle = fl_SetTitle;
    base->SetOkButtonLabel = fl_SetOkButtonLabel;
    base->SetFileNameLabel = fl_SetFileNameLabel;
    base->GetResult = fl_GetResult;
    base->AddPlace = fl_AddPlace;
    base->SetDefaultExtension = fl_SetDefaultExtension;
    base->Close = fl_Close;
    base->SetClientGuid = fl_SetClientGuid;
    base->ClearClientData = fl_ClearClientData;
    base->SetFilter = fl_SetFilter;
}

static void fl_init_vtbl(void)
{
    if (g_vtbl_ready) {
        return;
    }
    memset(&g_open_vtbl, 0, sizeof(g_open_vtbl));
    memset(&g_save_vtbl, 0, sizeof(g_save_vtbl));
    fl_fill_dialog_vtbl((IFileDialogVtbl *)&g_open_vtbl);
    g_open_vtbl.GetResults = fl_GetResults;
    g_open_vtbl.GetSelectedItems = fl_GetSelectedItems;
    fl_fill_dialog_vtbl((IFileDialogVtbl *)&g_save_vtbl);
    g_save_vtbl.SetSaveAsItem = fl_SetSaveAsItem;
    g_save_vtbl.SetProperties = fl_SetProperties;
    g_save_vtbl.SetCollectedProperties = fl_SetCollectedProperties;
    g_save_vtbl.GetProperties = fl_GetProperties;
    g_save_vtbl.ApplyProperties = fl_ApplyProperties;
    g_vtbl_ready = 1;
}

static struct fl_dlg *fl_from(IFileDialog *iface)
{
    return (struct fl_dlg *)iface;
}

static HRESULT fl_item_path(IShellItem *item, WCHAR **out)
{
    return item->lpVtbl->GetDisplayName(item, SIGDN_FILESYSPATH, out);
}

static HRESULT STDMETHODCALLTYPE fl_QueryInterface(IFileDialog *iface, REFIID riid, void **obj)
{
    struct fl_dlg *d = fl_from(iface);
    if (obj == NULL) {
        return E_POINTER;
    }
    *obj = NULL;
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IModalWindow) || IsEqualIID(riid, &IID_IFileDialog) ||
        (d->kind == FL_KIND_OPEN && IsEqualIID(riid, &IID_IFileOpenDialog)) ||
        (d->kind == FL_KIND_SAVE && IsEqualIID(riid, &IID_IFileSaveDialog))) {
        *obj = iface;
        fl_AddRef(iface);
        return S_OK;
    }
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE fl_AddRef(IFileDialog *iface)
{
    return (ULONG)InterlockedIncrement(&fl_from(iface)->refs);
}

static ULONG STDMETHODCALLTYPE fl_Release(IFileDialog *iface)
{
    struct fl_dlg *d = fl_from(iface);
    LONG n = InterlockedDecrement(&d->refs);
    if (n == 0) {
        fl_free_w(&d->title);
        fl_free_w(&d->folder);
        fl_free_w(&d->filename);
        fl_free_w(&d->defext);
        if (d->result != NULL) {
            d->result->lpVtbl->Release(d->result);
        }
        CoTaskMemFree(d);
    }
    return (ULONG)n;
}

static HRESULT STDMETHODCALLTYPE fl_Show(IFileDialog *iface, HWND owner)
{
    struct fl_dlg *d = fl_from(iface);
    const WCHAR *wverb;
    WCHAR *start_unix = NULL;
    char path[FL_PATH_MAX];
    HRESULT hr;
    (void)owner;
    if ((d->options & FOS_PICKFOLDERS) != 0) {
        wverb = L"pick-dir";
    } else if (d->kind == FL_KIND_SAVE) {
        wverb = L"save-file";
    } else {
        wverb = L"pick-file";
    }
    path[0] = '\0';
    start_unix = fl_win_to_unix_arg(d->folder);
    hr = fl_run_pick(wverb, start_unix != NULL ? start_unix : d->folder, d->filename, path, sizeof(path));
    free(start_unix);
    if (FAILED(hr)) {
        return hr;
    }
    return fl_set_result(d, path);
}

static HRESULT STDMETHODCALLTYPE fl_SetFileTypes(IFileDialog *iface, UINT c, const COMDLG_FILTERSPEC *f)
{
    (void)iface;
    (void)c;
    (void)f;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetFileTypeIndex(IFileDialog *iface, UINT i)
{
    (void)iface;
    (void)i;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_GetFileTypeIndex(IFileDialog *iface, UINT *i)
{
    if (i == NULL) {
        return E_POINTER;
    }
    (void)iface;
    *i = 1;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_Advise(IFileDialog *iface, IFileDialogEvents *e, DWORD *cookie)
{
    (void)iface;
    (void)e;
    if (cookie == NULL) {
        return E_POINTER;
    }
    *cookie = 1;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_Unadvise(IFileDialog *iface, DWORD cookie)
{
    (void)iface;
    (void)cookie;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetOptions(IFileDialog *iface, FILEOPENDIALOGOPTIONS fos)
{
    fl_from(iface)->options = fos;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_GetOptions(IFileDialog *iface, FILEOPENDIALOGOPTIONS *fos)
{
    if (fos == NULL) {
        return E_POINTER;
    }
    *fos = fl_from(iface)->options;
    return S_OK;
}

static HRESULT fl_store_folder(struct fl_dlg *d, IShellItem *item)
{
    WCHAR *path = NULL;
    HRESULT hr;
    if (item == NULL) {
        return E_INVALIDARG;
    }
    hr = fl_item_path(item, &path);
    if (FAILED(hr)) {
        return hr;
    }
    fl_free_w(&d->folder);
    d->folder = fl_wcsdup(path);
    CoTaskMemFree(path);
    return d->folder != NULL ? S_OK : E_OUTOFMEMORY;
}

static HRESULT STDMETHODCALLTYPE fl_SetDefaultFolder(IFileDialog *iface, IShellItem *item)
{
    return fl_store_folder(fl_from(iface), item);
}

static HRESULT STDMETHODCALLTYPE fl_SetFolder(IFileDialog *iface, IShellItem *item)
{
    return fl_store_folder(fl_from(iface), item);
}

static HRESULT STDMETHODCALLTYPE fl_GetFolder(IFileDialog *iface, IShellItem **item)
{
    (void)iface;
    if (item == NULL) {
        return E_POINTER;
    }
    *item = NULL;
    return E_NOTIMPL;
}

static HRESULT STDMETHODCALLTYPE fl_GetCurrentSelection(IFileDialog *iface, IShellItem **item)
{
    return fl_GetResult(iface, item);
}

static HRESULT STDMETHODCALLTYPE fl_SetFileName(IFileDialog *iface, LPCWSTR name)
{
    struct fl_dlg *d = fl_from(iface);
    fl_free_w(&d->filename);
    d->filename = fl_wcsdup(name);
    return d->filename != NULL || name == NULL ? S_OK : E_OUTOFMEMORY;
}

static HRESULT STDMETHODCALLTYPE fl_GetFileName(IFileDialog *iface, LPWSTR *name)
{
    struct fl_dlg *d = fl_from(iface);
    if (name == NULL) {
        return E_POINTER;
    }
    *name = fl_wcsdup(d->filename != NULL ? d->filename : L"");
    return *name != NULL ? S_OK : E_OUTOFMEMORY;
}

static HRESULT STDMETHODCALLTYPE fl_SetTitle(IFileDialog *iface, LPCWSTR title)
{
    struct fl_dlg *d = fl_from(iface);
    fl_free_w(&d->title);
    d->title = fl_wcsdup(title);
    return d->title != NULL || title == NULL ? S_OK : E_OUTOFMEMORY;
}

static HRESULT STDMETHODCALLTYPE fl_SetOkButtonLabel(IFileDialog *iface, LPCWSTR label)
{
    (void)iface;
    (void)label;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetFileNameLabel(IFileDialog *iface, LPCWSTR label)
{
    (void)iface;
    (void)label;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_GetResult(IFileDialog *iface, IShellItem **item)
{
    struct fl_dlg *d = fl_from(iface);
    if (item == NULL) {
        return E_POINTER;
    }
    if (d->result == NULL) {
        *item = NULL;
        return E_UNEXPECTED;
    }
    d->result->lpVtbl->AddRef(d->result);
    *item = d->result;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_AddPlace(IFileDialog *iface, IShellItem *item, FDAP fdap)
{
    (void)iface;
    (void)item;
    (void)fdap;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetDefaultExtension(IFileDialog *iface, LPCWSTR ext)
{
    struct fl_dlg *d = fl_from(iface);
    fl_free_w(&d->defext);
    d->defext = fl_wcsdup(ext);
    return d->defext != NULL || ext == NULL ? S_OK : E_OUTOFMEMORY;
}

static HRESULT STDMETHODCALLTYPE fl_Close(IFileDialog *iface, HRESULT hr)
{
    (void)iface;
    (void)hr;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetClientGuid(IFileDialog *iface, REFGUID guid)
{
    (void)iface;
    (void)guid;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_ClearClientData(IFileDialog *iface)
{
    (void)iface;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetFilter(IFileDialog *iface, IShellItemFilter *filter)
{
    (void)iface;
    (void)filter;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_GetResults(IFileOpenDialog *iface, IShellItemArray **items)
{
    IShellItem *one = NULL;
    HRESULT hr;
    if (items == NULL) {
        return E_POINTER;
    }
    hr = fl_GetResult((IFileDialog *)iface, &one);
    if (FAILED(hr)) {
        return hr;
    }
    hr = SHCreateShellItemArrayFromShellItem(one, &IID_IShellItemArray, (void **)items);
    one->lpVtbl->Release(one);
    return hr;
}

static HRESULT STDMETHODCALLTYPE fl_GetSelectedItems(IFileOpenDialog *iface, IShellItemArray **items)
{
    return fl_GetResults(iface, items);
}

static HRESULT STDMETHODCALLTYPE fl_SetSaveAsItem(IFileSaveDialog *iface, IShellItem *item)
{
    return fl_store_folder(fl_from((IFileDialog *)iface), item);
}

static HRESULT STDMETHODCALLTYPE fl_SetProperties(IFileSaveDialog *iface, IPropertyStore *store)
{
    (void)iface;
    (void)store;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_SetCollectedProperties(IFileSaveDialog *iface, IPropertyDescriptionList *list, BOOL append)
{
    (void)iface;
    (void)list;
    (void)append;
    return S_OK;
}

static HRESULT STDMETHODCALLTYPE fl_GetProperties(IFileSaveDialog *iface, IPropertyStore **store)
{
    (void)iface;
    if (store == NULL) {
        return E_POINTER;
    }
    *store = NULL;
    return E_NOTIMPL;
}

static HRESULT STDMETHODCALLTYPE fl_ApplyProperties(IFileSaveDialog *iface, IShellItem *item, IPropertyStore *store, HWND hwnd, IFileOperationProgressSink *sink)
{
    (void)iface;
    (void)item;
    (void)store;
    (void)hwnd;
    (void)sink;
    return S_OK;
}

static HRESULT fl_dlg_new(int kind, REFIID riid, void **obj)
{
    struct fl_dlg *d;
    HRESULT hr;
    fl_init_vtbl();
    d = (struct fl_dlg *)CoTaskMemAlloc(sizeof(*d));
    if (d == NULL) {
        return E_OUTOFMEMORY;
    }
    memset(d, 0, sizeof(*d));
    d->vtbl = kind == FL_KIND_SAVE ? (IFileDialogVtbl *)&g_save_vtbl : (IFileDialogVtbl *)&g_open_vtbl;
    d->refs = 1;
    d->kind = kind;
    d->options = FOS_PATHMUSTEXIST | FOS_FILEMUSTEXIST;
    if (kind == FL_KIND_SAVE) {
        d->options = FOS_PATHMUSTEXIST | FOS_OVERWRITEPROMPT;
    }
    hr = fl_QueryInterface((IFileDialog *)d, riid, obj);
    fl_Release((IFileDialog *)d);
    return hr;
}

/* ---- class factory ------------------------------------------------------ */

static HRESULT STDMETHODCALLTYPE cf_QueryInterface(IClassFactory *iface, REFIID riid, void **obj);
static ULONG STDMETHODCALLTYPE cf_AddRef(IClassFactory *iface);
static ULONG STDMETHODCALLTYPE cf_Release(IClassFactory *iface);
static HRESULT STDMETHODCALLTYPE cf_CreateInstance(IClassFactory *iface, IUnknown *outer, REFIID riid, void **obj);
static HRESULT STDMETHODCALLTYPE cf_LockServer(IClassFactory *iface, BOOL lock);

static IClassFactoryVtbl g_cf_vtbl = {
    cf_QueryInterface,
    cf_AddRef,
    cf_Release,
    cf_CreateInstance,
    cf_LockServer,
};

static HRESULT STDMETHODCALLTYPE cf_QueryInterface(IClassFactory *iface, REFIID riid, void **obj)
{
    if (obj == NULL) {
        return E_POINTER;
    }
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IClassFactory)) {
        *obj = iface;
        cf_AddRef(iface);
        return S_OK;
    }
    *obj = NULL;
    return E_NOINTERFACE;
}

static ULONG STDMETHODCALLTYPE cf_AddRef(IClassFactory *iface)
{
    return (ULONG)InterlockedIncrement(&((struct fl_factory *)iface)->refs);
}

static ULONG STDMETHODCALLTYPE cf_Release(IClassFactory *iface)
{
    struct fl_factory *f = (struct fl_factory *)iface;
    LONG n = InterlockedDecrement(&f->refs);
    if (n == 0) {
        CoTaskMemFree(f);
    }
    return (ULONG)n;
}

static HRESULT STDMETHODCALLTYPE cf_CreateInstance(IClassFactory *iface, IUnknown *outer, REFIID riid, void **obj)
{
    struct fl_factory *f = (struct fl_factory *)iface;
    if (outer != NULL) {
        return CLASS_E_NOAGGREGATION;
    }
    return fl_dlg_new(f->kind, riid, obj);
}

static HRESULT STDMETHODCALLTYPE cf_LockServer(IClassFactory *iface, BOOL lock)
{
    (void)iface;
    if (lock) {
        InterlockedIncrement(&g_locks);
    } else {
        InterlockedDecrement(&g_locks);
    }
    return S_OK;
}

/* ---- exports ------------------------------------------------------------ */

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved);
HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void **obj);
HRESULT WINAPI DllCanUnloadNow(void);

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = instance;
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}

HRESULT WINAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void **obj)
{
    struct fl_factory *f;
    int kind;
    if (obj == NULL) {
        return E_POINTER;
    }
    *obj = NULL;
    if (IsEqualCLSID(clsid, &CLSID_FileOpenDialog_fl)) {
        kind = FL_KIND_OPEN;
    } else if (IsEqualCLSID(clsid, &CLSID_FileSaveDialog_fl)) {
        kind = FL_KIND_SAVE;
    } else {
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    f = (struct fl_factory *)CoTaskMemAlloc(sizeof(*f));
    if (f == NULL) {
        return E_OUTOFMEMORY;
    }
    f->vtbl = &g_cf_vtbl;
    f->refs = 1;
    f->kind = kind;
    {
        HRESULT hr = cf_QueryInterface((IClassFactory *)f, riid, obj);
        cf_Release((IClassFactory *)f);
        return hr;
    }
}

HRESULT WINAPI DllCanUnloadNow(void)
{
    return g_locks == 0 ? S_OK : S_FALSE;
}
