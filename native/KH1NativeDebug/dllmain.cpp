#include "pch.h"
#include <tlhelp32.h>
#include <cstdio>
#include <cstring>
#include <d3d11.h>

#include "imgui/imgui.h"
#include "imgui/imgui_impl_win32.h"
#include "imgui/imgui_impl_dx11.h"

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

// This is the debug-only half of kh1_native.dll (see KH1-LUA-LIBRARY), split
// out so normal players never download or load the ImGui overlay -- only
// scripts/kh1_native_test.lua in this repo drives it (poll_debug_action is
// never called otherwise, so the window can never appear without it).
// The real game-calling bridge (call_function, write_floats, popup hooks)
// stays in kh1_native.dll; this module only queues named debug actions for
// kh1_native_test.lua to dispatch through the real Lua library functions.

// --- DEBUG LOGGING ---
static char g_dllDir[MAX_PATH] = "";

static void LogDebug(const char* msg) {
    if (!g_dllDir[0]) return;
    char path[MAX_PATH];
    snprintf(path, MAX_PATH, "%skh1_native_debug.log", g_dllDir);
    FILE* f = nullptr;
    if (fopen_s(&f, path, "a") == 0 && f) {
        SYSTEMTIME st;
        GetLocalTime(&st);
        fprintf(f, "[%02d:%02d:%02d] %s\n", st.wHour, st.wMinute, st.wSecond, msg);
        fclose(f);
    }
}

// --- LUA FUNCTION POINTERS ---
// Resolved against whichever already-loaded module in the host process exports
// the Lua C API (no Lua headers needed) -- see FindLuaModule(). Duplicated
// from kh1_native.dll's own copy rather than shared, since this module is
// intentionally independent of it (only the small subset this module's own
// Lua-callable functions actually need).
typedef int          (__cdecl* t_lua_gettop)(void* L);
typedef const char*  (__cdecl* t_lua_tolstring)(void* L, int idx, size_t* len);
typedef void         (__cdecl* t_lua_pushinteger)(void* L, long long n);
typedef void         (__cdecl* t_lua_pushnumber)(void* L, double n);
typedef const char*  (__cdecl* t_lua_pushstring)(void* L, const char* s);
typedef void         (__cdecl* t_luaL_setfuncs)(void* L, const void* l, int nup);
typedef void         (__cdecl* t_lua_createtable)(void* L, int narr, int nrec);
typedef void         (__cdecl* t_lua_setfield)(void* L, int idx, const char* k);
typedef void         (__cdecl* t_lua_rawseti)(void* L, int idx, long long n);

static t_lua_gettop       p_lua_gettop       = nullptr;
static t_lua_tolstring    p_lua_tolstring    = nullptr;
static t_lua_pushinteger  p_lua_pushinteger  = nullptr;
static t_lua_pushnumber   p_lua_pushnumber   = nullptr;
static t_lua_pushstring   p_lua_pushstring   = nullptr;
static t_luaL_setfuncs    p_luaL_setfuncs    = nullptr;
static t_lua_createtable  p_lua_createtable  = nullptr;
static t_lua_setfield     p_lua_setfield     = nullptr;
static t_lua_rawseti      p_lua_rawseti      = nullptr;

struct luaL_Reg { const char* name; void* func; };

// --- DEBUG OVERLAY <-> LUA SHARED STATE ---
// Bridges the overlay's own UI thread with Lua's _OnFrame. Guarded by g_lock
// since the two run on different threads. The overlay can't call game
// functions directly from its own thread -- calling game code from any
// thread other than Lua's (which runs on the game's own main thread) is
// unreliable -- so button clicks only ever queue a request; the actual call
// happens in kh1_native_test.lua's _OnFrame, after polling it via
// poll_debug_action.
static SRWLOCK g_lock = SRWLOCK_INIT;

static bool g_debugActionPending = false;
static char g_debugAction[32] = "";
static long long g_debugParam1 = 0; // window_id, used by every text-box action
// duration, style, x, y, width, height -- all six live in one array so the
// overlay can queue "open a text box with every current field" in a single
// action rather than needing a separate Set Style/Position/Size step first.
static double g_debugNums[6] = { 0.0, 0.0, 0.0, 0.0, 0.0, 0.0 };
static char g_debugParamText[256] = "";

static char g_debugResult[256] = "";

// --- STANDALONE IMGUI WINDOW ---
// Its own window, own D3D11 device, own swap chain, own message/render loop --
// completely independent of the game's window and renderer, so this can't
// destabilize the game the way hooking its renderer would. Same approach as
// KH1Overlay in the randomizer repo (duplicated rather than shared, since
// this module is intentionally independent of it).
static HWND g_hwnd = nullptr;
static ID3D11Device* g_device = nullptr;
static ID3D11DeviceContext* g_context = nullptr;
static IDXGISwapChain* g_swapChain = nullptr;
static ID3D11RenderTargetView* g_rtv = nullptr;
static LONG g_formThreadStarted = 0;
static bool g_formVisible = false;
static volatile bool g_shuttingDown = false;

static void CreateRenderTarget() {
    ID3D11Texture2D* backBuffer = nullptr;
    g_swapChain->GetBuffer(0, __uuidof(ID3D11Texture2D), reinterpret_cast<void**>(&backBuffer));
    g_device->CreateRenderTargetView(backBuffer, nullptr, &g_rtv);
    backBuffer->Release();
}

static LRESULT CALLBACK FormWndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    if (ImGui_ImplWin32_WndProcHandler(hwnd, msg, wParam, lParam)) {
        return true;
    }
    switch (msg) {
    case WM_CLOSE:
        ShowWindow(hwnd, SW_HIDE);
        g_formVisible = false;
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    case WM_GETMINMAXINFO: {
        // Keeps the two-pane list/detail layout from collapsing into an
        // unusable sliver if the window gets shrunk.
        MINMAXINFO* mmi = reinterpret_cast<MINMAXINFO*>(lParam);
        mmi->ptMinTrackSize.x = 640;
        mmi->ptMinTrackSize.y = 420;
        return 0;
    }
    case WM_SIZE:
        if (g_swapChain && wParam != SIZE_MINIMIZED) {
            if (g_rtv) { g_rtv->Release(); g_rtv = nullptr; }
            g_swapChain->ResizeBuffers(0, LOWORD(lParam), HIWORD(lParam), DXGI_FORMAT_UNKNOWN, 0);
            CreateRenderTarget();
        }
        return 0;
    }
    return DefWindowProcA(hwnd, msg, wParam, lParam);
}

// --- STYLE ---
// Applied once after ImGui::CreateContext(). Purely cosmetic -- the point is
// to make the fullscreen-docked layout below (list + detail pane) read as one
// coherent app surface rather than default ImGui demo styling.
static void ApplyImGuiStyle() {
    ImGui::StyleColorsDark();
    ImGuiStyle& style = ImGui::GetStyle();
    style.WindowRounding = 0.0f;
    style.ChildRounding = 6.0f;
    style.FrameRounding = 4.0f;
    style.GrabRounding = 4.0f;
    style.ScrollbarRounding = 6.0f;
    style.WindowPadding = ImVec2(14, 14);
    style.FramePadding = ImVec2(8, 6);
    style.ItemSpacing = ImVec2(8, 8);
    style.ScrollbarSize = 14.0f;

    ImVec4* colors = style.Colors;
    colors[ImGuiCol_WindowBg] = ImVec4(0.10f, 0.10f, 0.12f, 1.00f);
    colors[ImGuiCol_ChildBg] = ImVec4(0.14f, 0.14f, 0.17f, 1.00f);
    colors[ImGuiCol_Border] = ImVec4(0.25f, 0.25f, 0.30f, 0.60f);
    colors[ImGuiCol_Header] = ImVec4(0.20f, 0.45f, 0.75f, 0.55f);
    colors[ImGuiCol_HeaderHovered] = ImVec4(0.25f, 0.55f, 0.85f, 0.75f);
    colors[ImGuiCol_HeaderActive] = ImVec4(0.25f, 0.55f, 0.85f, 1.00f);
    colors[ImGuiCol_Button] = ImVec4(0.20f, 0.35f, 0.55f, 1.00f);
    colors[ImGuiCol_ButtonHovered] = ImVec4(0.25f, 0.45f, 0.70f, 1.00f);
    colors[ImGuiCol_ButtonActive] = ImVec4(0.20f, 0.55f, 0.85f, 1.00f);
}

// --- FUNCTION REGISTRY ---
// One row per queueable debug action. Drives the left-hand list; DrawForm's
// detail pane still switches on `id` to render that action's specific fields,
// but adding an entry here is what makes a new action show up in the list at
// all (grouped under `category`, in array order).
struct DebugFunctionEntry {
    const char* id;
    const char* category;
    const char* name;
};

static const DebugFunctionEntry g_debugFunctions[] = {
    { "spawn_prize",       "Items",    "Spawn Prize" },
    { "show_custom_popup", "Popups",   "Show Custom Popup" },
    { "open_text_box",     "Text Box", "Open Text Box" },
    { "close_text_box",    "Text Box", "Close Text Box" },
    { "play_se2",          "Audio",    "Play SE2" },
};
static const int kDebugFunctionCount = static_cast<int>(sizeof(g_debugFunctions) / sizeof(g_debugFunctions[0]));
static int g_selectedFunction = 0;

static void QueueDebugAction(const char* action, long long param1, const char* text, const double nums[6]) {
    AcquireSRWLockExclusive(&g_lock);
    strncpy_s(g_debugAction, action, _TRUNCATE);
    g_debugParam1 = param1;
    strncpy_s(g_debugParamText, text ? text : "", _TRUNCATE);
    if (nums) memcpy(g_debugNums, nums, sizeof(g_debugNums));
    else memset(g_debugNums, 0, sizeof(g_debugNums));
    g_debugActionPending = true;
    ReleaseSRWLockExclusive(&g_lock);
}

// Full-width, extra-tall button so "call this function" is always the
// unmistakable, unambiguous action in the detail pane.
static bool BigCallButton(const char* label = "Call") {
    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.20f, 0.55f, 0.30f, 1.0f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.25f, 0.65f, 0.35f, 1.0f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(0.15f, 0.45f, 0.25f, 1.0f));
    bool clicked = ImGui::Button(label, ImVec2(-1, 46));
    ImGui::PopStyleColor(3);
    return clicked;
}

// Only known/named actions are exposed here (not a raw address+args form) --
// each button queues a specific, already-vetted request for Lua to dispatch
// through the real named Lua function (e.g. spawn_prize), which is what picks
// the correct Steam/EGS address.
//
// Layout: the ImGui window is pinned to fill the whole OS window every frame
// (no title bar/move/resize of its own) so there's exactly one window, not a
// draggable pane floating inside another one. Inside it: a scrollable list of
// functions on the left, and the selected function's parameters + a big Call
// button on the right.
static void DrawForm() {
    ImGuiViewport* viewport = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(viewport->WorkPos);
    ImGui::SetNextWindowSize(viewport->WorkSize);
    ImGui::Begin("KH1Native Debug", nullptr,
        ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
        ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoBringToFrontOnFocus);

    ImGui::TextColored(ImVec4(0.60f, 0.75f, 1.00f, 1.0f), "KH1Native Debug");
    ImGui::Separator();
    ImGui::Spacing();

    const float statusHeight = ImGui::GetTextLineHeightWithSpacing() * 2.0f;
    ImVec2 avail = ImGui::GetContentRegionAvail();
    float bodyHeight = avail.y - statusHeight - ImGui::GetStyle().ItemSpacing.y;

    // --- Left: scrollable function list ---
    ImGui::BeginChild("FunctionList", ImVec2(220, bodyHeight), true);
    const char* lastCategory = nullptr;
    for (int i = 0; i < kDebugFunctionCount; ++i) {
        const DebugFunctionEntry& entry = g_debugFunctions[i];
        if (!lastCategory || strcmp(lastCategory, entry.category) != 0) {
            ImGui::SeparatorText(entry.category);
            lastCategory = entry.category;
        }
        if (ImGui::Selectable(entry.name, g_selectedFunction == i, 0, ImVec2(0, 24))) {
            g_selectedFunction = i;
        }
    }
    ImGui::EndChild();

    ImGui::SameLine();

    // --- Right: selected function's parameters + call button ---
    ImGui::BeginChild("FunctionDetail", ImVec2(0, bodyHeight), true);
    const DebugFunctionEntry& current = g_debugFunctions[g_selectedFunction];
    ImGui::TextColored(ImVec4(0.60f, 0.75f, 1.00f, 1.0f), "%s", current.name);
    ImGui::Separator();
    ImGui::Spacing();

    if (strcmp(current.id, "spawn_prize") == 0) {
        static int itemId = 1;
        ImGui::InputInt("Item ID", &itemId);
        if (itemId < 1) itemId = 1;
        if (BigCallButton()) {
            QueueDebugAction("spawn_prize", itemId, nullptr, nullptr);
        }
    } else if (strcmp(current.id, "show_custom_popup") == 0) {
        static char customText[128] = "TEST";
        ImGui::InputText("Popup Text", customText, sizeof(customText));
        if (BigCallButton()) {
            QueueDebugAction("show_custom_popup", 0, customText, nullptr);
        }
    } else if (strcmp(current.id, "open_text_box") == 0) {
        static char textBoxText[128] = "TEST TEXT BOX";
        static int textBoxWindowId = 1;
        static float textBoxDuration = 0.0f;
        static int textBoxStyle = 0;
        static int textBoxX = 0;
        static int textBoxY = 0;
        static int textBoxWidth = 10;
        static int textBoxHeight = 3;

        ImGui::InputText("Text Box Text", textBoxText, sizeof(textBoxText));
        ImGui::InputInt("Window ID", &textBoxWindowId);
        if (textBoxWindowId < 0) textBoxWindowId = 0;
        if (textBoxWindowId > 3) textBoxWindowId = 3;
        ImGui::InputFloat("Duration (seconds, 0=manual)", &textBoxDuration);
        if (textBoxDuration < 0.0f) textBoxDuration = 0.0f;
        ImGui::InputInt("Style (raw, 0-8 valid)", &textBoxStyle);
        if (textBoxStyle < 0) textBoxStyle = 0;
        if (textBoxStyle > 8) textBoxStyle = 8;
        ImGui::InputInt("X", &textBoxX);
        ImGui::InputInt("Y", &textBoxY);
        ImGui::InputInt("Width", &textBoxWidth);
        ImGui::InputInt("Height", &textBoxHeight);

        // Single action carries every field at once -- kh1_lua_library's
        // open_text_box applies style/position/size to the template before
        // opening, so there's no separate "configure, then open" step here.
        if (BigCallButton("Open Text Box")) {
            double nums[6] = {
                (double)textBoxDuration, (double)textBoxStyle, (double)textBoxX,
                (double)textBoxY, (double)textBoxWidth, (double)textBoxHeight
            };
            QueueDebugAction("open_text_box", textBoxWindowId, textBoxText, nums);
        }
    } else if (strcmp(current.id, "close_text_box") == 0) {
        static int textBoxWindowId = 1;
        ImGui::InputInt("Window ID", &textBoxWindowId);
        if (textBoxWindowId < 0) textBoxWindowId = 0;
        if (textBoxWindowId > 3) textBoxWindowId = 3;
        if (BigCallButton("Close Text Box")) {
            QueueDebugAction("close_text_box", textBoxWindowId, nullptr, nullptr);
        }
    } else if (strcmp(current.id, "play_se2") == 0) {
        static int seId = 31;
        ImGui::InputInt("SE ID (valid range ~1-76)", &seId);
        if (seId < 1) seId = 1;
        if (seId > 76) seId = 76;
        ImGui::TextWrapped("Param 2 is always 0 -- unregistered SE ids outside this range can crash the game.");
        if (BigCallButton("Play SE2")) {
            double nums[6] = { (double)seId, 0.0, 0.0, 0.0, 0.0, 0.0 };
            QueueDebugAction("play_se2", 0, nullptr, nums);
        }
    }

    ImGui::EndChild();

    // --- Status bar ---
    char result[256];
    AcquireSRWLockExclusive(&g_lock);
    strncpy_s(result, g_debugResult, _TRUNCATE);
    ReleaseSRWLockExclusive(&g_lock);

    ImGui::Spacing();
    ImGui::TextWrapped("Last result: %s", result[0] ? result : "(none yet)");

    ImGui::End();
}

static DWORD WINAPI FormThread(LPVOID) {
    WNDCLASSEXA wc = {};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = FormWndProc;
    wc.hInstance = GetModuleHandleA(nullptr);
    wc.lpszClassName = "KH1NativeDebugWndClass";
    wc.hCursor = LoadCursorA(nullptr, reinterpret_cast<LPCSTR>(IDC_ARROW));
    RegisterClassExA(&wc);

    g_hwnd = CreateWindowExA(WS_EX_TOPMOST, wc.lpszClassName, "KH1Native Debug",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 820, 480, nullptr, nullptr, wc.hInstance, nullptr);

    DXGI_SWAP_CHAIN_DESC scd = {};
    scd.BufferCount = 2;
    scd.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    scd.OutputWindow = g_hwnd;
    scd.SampleDesc.Count = 1;
    scd.Windowed = TRUE;
    scd.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    D3D_FEATURE_LEVEL level;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0,
        nullptr, 0, D3D11_SDK_VERSION, &scd, &g_swapChain, &g_device, &level, &g_context);

    char msg[128];
    snprintf(msg, sizeof(msg), "Form window D3D11CreateDeviceAndSwapChain hr=0x%08lX", static_cast<unsigned long>(hr));
    LogDebug(msg);
    if (FAILED(hr)) return 0;

    CreateRenderTarget();

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    ApplyImGuiStyle();

    ImGui_ImplWin32_Init(g_hwnd);
    ImGui_ImplDX11_Init(g_device, g_context);

    LogDebug("Debug form window + standalone ImGui context ready");

    while (!g_shuttingDown) {
        MSG msg2;
        while (PeekMessageA(&msg2, nullptr, 0, 0, PM_REMOVE)) {
            if (msg2.message == WM_QUIT) {
                g_shuttingDown = true;
                break;
            }
            TranslateMessage(&msg2);
            DispatchMessageA(&msg2);
        }
        if (g_shuttingDown) break;

        if (!g_formVisible) {
            Sleep(50);
            continue;
        }

        ImGui_ImplDX11_NewFrame();
        ImGui_ImplWin32_NewFrame();
        ImGui::NewFrame();
        DrawForm();
        ImGui::Render();

        const float clearColor[4] = { 0.10f, 0.10f, 0.12f, 1.0f };
        g_context->OMSetRenderTargets(1, &g_rtv, nullptr);
        g_context->ClearRenderTargetView(g_rtv, clearColor);
        ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());

        g_swapChain->Present(1, 0);
        Sleep(16);
    }

    // Reached on WM_QUIT or when DllMain(DLL_PROCESS_DETACH) asks us to stop --
    // either way, fully tear down before this thread returns so nothing is left
    // executing inside this DLL's code if/when it gets unloaded out from under us.
    LogDebug("Debug form window thread shutting down");
    ImGui_ImplDX11_Shutdown();
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    if (g_rtv) { g_rtv->Release(); g_rtv = nullptr; }
    if (g_swapChain) { g_swapChain->Release(); g_swapChain = nullptr; }
    if (g_context) { g_context->Release(); g_context = nullptr; }
    if (g_device) { g_device->Release(); g_device = nullptr; }
    if (g_hwnd) { DestroyWindow(g_hwnd); g_hwnd = nullptr; }
    return 0;
}

static void EnsureFormThreadStarted() {
    if (InterlockedCompareExchange(&g_formThreadStarted, 1, 0) == 0) {
        LogDebug("Spawning debug form window thread");
        CreateThread(nullptr, 0, FormThread, nullptr, 0, nullptr);
    }
}

static void ToggleFormVisibility() {
    EnsureFormThreadStarted();

    // First press only: the form thread needs a moment to create the window.
    for (int i = 0; i < 50 && !g_hwnd; ++i) {
        Sleep(10);
    }
    if (!g_hwnd) return;

    g_formVisible = !g_formVisible;
    if (g_formVisible) {
        ShowWindow(g_hwnd, SW_SHOW);
        SetForegroundWindow(g_hwnd);
    } else {
        ShowWindow(g_hwnd, SW_HIDE);
    }
}

// --- LUA-CALLABLE FUNCTIONS ---

// poll_debug_action() -> nil | {action=, param1=, nums={duration,style,x,y,width,height}, param_text=}
//
// Called every Lua frame by the debug companion script. Also polls F6 to
// show/hide the debug window (same edge-triggered pattern KH1Overlay uses for
// F4), since the window's own thread can't safely call game functions itself.
extern "C" int l_poll_debug_action(void* L) {
    static bool lastF6 = false;
    bool currF6 = (GetAsyncKeyState(VK_F6) & 0x8000) != 0;
    if (currF6 && !lastF6) {
        ToggleFormVisibility();
    }
    lastF6 = currF6;

    bool has;
    char action[32];
    long long param1;
    double nums[6];
    char paramText[256];
    AcquireSRWLockExclusive(&g_lock);
    has = g_debugActionPending;
    if (has) {
        strncpy_s(action, g_debugAction, _TRUNCATE);
        param1 = g_debugParam1;
        memcpy(nums, g_debugNums, sizeof(nums));
        strncpy_s(paramText, g_debugParamText, _TRUNCATE);
        g_debugActionPending = false;
    }
    ReleaseSRWLockExclusive(&g_lock);

    if (!has) return 0;

    p_lua_createtable(L, 0, 4);
    p_lua_pushstring(L, action); p_lua_setfield(L, -2, "action");
    p_lua_pushinteger(L, param1); p_lua_setfield(L, -2, "param1");
    p_lua_pushstring(L, paramText); p_lua_setfield(L, -2, "param_text");

    p_lua_createtable(L, 6, 0);
    for (int i = 0; i < 6; ++i) {
        p_lua_pushnumber(L, nums[i]);
        p_lua_rawseti(L, -2, i + 1);
    }
    p_lua_setfield(L, -2, "nums");

    return 1;
}

// set_debug_result(text) -> (none)
// Called by the debug companion script after dispatching a polled action, so
// the overlay window has something to show for what just happened.
extern "C" int l_set_debug_result(void* L) {
    const char* text = p_lua_tolstring(L, 1, nullptr);
    AcquireSRWLockExclusive(&g_lock);
    strncpy_s(g_debugResult, text ? text : "", _TRUNCATE);
    ReleaseSRWLockExclusive(&g_lock);
    return 0;
}

static const luaL_Reg kh1_native_debug_lib[] = {
    {"poll_debug_action", reinterpret_cast<void*>(l_poll_debug_action)},
    {"set_debug_result", reinterpret_cast<void*>(l_set_debug_result)},
    {nullptr, nullptr}
};

// Every Lua C API export this module needs to bridge into the host's Lua
// state. A candidate module only counts if ALL of these resolve from it.
static const char* const kRequiredLuaExports[] = {
    "lua_gettop", "lua_tolstring", "lua_pushinteger", "lua_pushnumber",
    "lua_pushstring", "luaL_setfuncs", "lua_createtable", "lua_setfield",
    "lua_rawseti",
};

static bool ModuleExportsAllRequired(HMODULE mod) {
    if (!mod) return false;
    for (const char* name : kRequiredLuaExports) {
        if (!GetProcAddress(mod, name)) return false;
    }
    return true;
}

// Last-resort fallback in case the bundled lua54.dll (see FindLuaModule)
// somehow isn't loaded: scan every module in the process, requiring ALL
// required symbols to resolve from the SAME module before accepting it.
static HMODULE FindLuaModuleByProcessScan() {
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, GetCurrentProcessId());
    if (snap == INVALID_HANDLE_VALUE) return nullptr;

    HMODULE found = nullptr;
    MODULEENTRY32W me = {};
    me.dwSize = sizeof(me);
    if (Module32FirstW(snap, &me)) {
        do {
            if (ModuleExportsAllRequired(me.hModule)) {
                found = me.hModule;
                char msg[MAX_PATH + 32];
                snprintf(msg, sizeof(msg), "FindLuaModuleByProcessScan: found Lua API in module: %ls", me.szModule);
                LogDebug(msg);
                break;
            }
        } while (Module32NextW(snap, &me));
    }
    CloseHandle(snap);
    return found;
}

// The current LuaBackend build statically embeds Lua 5.4 as private,
// unexported code -- there is no external door into it by any technique, so
// this relies on kh1_lua_library (KH1-LUA-LIBRARY, always installed
// alongside this debug tool) bundling its own known-good lua54.dll directly
// under dll/lua54.dll, loaded automatically by Panacea before any script
// runs. This module doesn't bundle a second copy of its own -- it just looks
// for that same, already-guaranteed-present module by name.
static HMODULE FindLuaModule() {
    HMODULE bundled = GetModuleHandleA("lua54.dll");
    if (ModuleExportsAllRequired(bundled)) {
        LogDebug("FindLuaModule: resolved via bundled lua54.dll (from kh1-lua-library)");
        return bundled;
    }

    LogDebug("FindLuaModule: bundled lua54.dll not found or incomplete, falling back to process scan");
    return FindLuaModuleByProcessScan();
}

extern "C" __declspec(dllexport) int luaopen_kh1_native_debug(void* L) {
    LogDebug("luaopen_kh1_native_debug called");

    HMODULE hLua = FindLuaModule();
    if (hLua && !p_lua_gettop) {
        p_lua_gettop      = (t_lua_gettop)      GetProcAddress(hLua, "lua_gettop");
        p_lua_tolstring   = (t_lua_tolstring)   GetProcAddress(hLua, "lua_tolstring");
        p_lua_pushinteger = (t_lua_pushinteger) GetProcAddress(hLua, "lua_pushinteger");
        p_lua_pushnumber  = (t_lua_pushnumber)  GetProcAddress(hLua, "lua_pushnumber");
        p_lua_pushstring  = (t_lua_pushstring)  GetProcAddress(hLua, "lua_pushstring");
        p_luaL_setfuncs   = (t_luaL_setfuncs)   GetProcAddress(hLua, "luaL_setfuncs");
        p_lua_createtable = (t_lua_createtable) GetProcAddress(hLua, "lua_createtable");
        p_lua_setfield    = (t_lua_setfield)    GetProcAddress(hLua, "lua_setfield");
        p_lua_rawseti     = (t_lua_rawseti)     GetProcAddress(hLua, "lua_rawseti");
    }

    if (!p_lua_gettop || !p_lua_tolstring || !p_lua_pushinteger || !p_lua_pushnumber || !p_lua_pushstring ||
        !p_luaL_setfuncs || !p_lua_createtable || !p_lua_setfield || !p_lua_rawseti) {
        // Couldn't find a loaded module exporting the Lua C API -- bail out
        // without touching any of them. Returning 0 (no pushed values) makes
        // require() hand back `true` rather than crashing on a null function
        // pointer call.
        LogDebug("luaopen_kh1_native_debug: failed to resolve Lua API exports, aborting safely");
        return 0;
    }

    p_lua_createtable(L, 0, 2);
    p_luaL_setfuncs(L, kh1_native_debug_lib, 0);
    return 1;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID lpReserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        GetModuleFileNameA(hModule, g_dllDir, MAX_PATH);
        char* last = strrchr(g_dllDir, '\\');
        if (last) *(last + 1) = '\0';

        // Pin ourselves in memory with an extra reference we never release.
        // LuaBackend's script-refresh feature appears to FreeLibrary() native
        // modules it required as part of giving scripts a clean reload -- if
        // the debug form thread is still running at that exact moment, having
        // this DLL's code unmapped out from under it is an instant crash, and
        // waiting for the thread to exit from DLL_PROCESS_DETACH risks a
        // loader-lock deadlock instead (this is the same issue KH1Overlay's
        // dllmain.cpp documents and works around the same way). Holding an
        // extra reference means an external FreeLibrary() call just decrements
        // our refcount instead of actually unloading us, so the thread is
        // never disturbed and DLL_PROCESS_DETACH is never reached mid-session.
        char selfPath[MAX_PATH];
        GetModuleFileNameA(hModule, selfPath, MAX_PATH);
        LoadLibraryA(selfPath);
    } else if (reason == DLL_PROCESS_DETACH) {
        // Only reached on real process shutdown now. When lpReserved is
        // non-null the process is terminating and other threads may already
        // be gone, so per Microsoft's own guidance we must not synchronize
        // with anything here -- just let the OS reclaim everything.
        (void)lpReserved;
        g_shuttingDown = true;
    }
    return TRUE;
}
