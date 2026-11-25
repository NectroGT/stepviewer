"""Entry point used by the desktop app (desktop/main.js), packaged with PyInstaller into pdf2step-server.exe.

It runs the same web app as app.py, but:
  - listens on 127.0.0.1 only, on a free port, and prints "READY <port>" once it is listening;
  - only answers requests carrying the secret key the desktop app passed in PDF2STEP_TOKEN: the app opens
    /?key=<token> once, which sets a cookie, so other programs and web pages on this computer get 403;
  - quits (with its worker processes) when the desktop app's process (PDF2STEP_PARENT_PID) ends, even if it crashed.
Uploads and results go to PDF2STEP_JOBS (the app's user folder)."""
import multiprocessing, os, secrets, sys, threading


def kill_workers_with_me():
    """Put this process in a Windows job object that kills everything in it when this process ends.
    The worker processes it starts join the job automatically, so they can never be left running on
    their own, however the converter ends (app quit, crash, Task Manager)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return
    info = EXTENDED_LIMITS()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))   # 9 = ExtendedLimitInformation
    if ok:
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
    # the handle is deliberately never closed: Windows closes it when this process ends, which kills the job


def watch_parent(pid):
    """Quit when the desktop app exits or crashes. Waits on the app's process handle; it must not block
    reading stdin: on Windows a thread stuck in a pipe read stops new worker processes from starting."""
    if not pid or sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)            # SYNCHRONIZE
    if not h:
        return

    def wait():
        k32.WaitForSingleObject(h, 0xFFFFFFFF)            # INFINITE: returns when the app's process ends
        os._exit(0)                                        # the job object then ends the worker processes

    threading.Thread(target=wait, daemon=True).start()


def main():
    import app as web
    from flask import abort, make_response, redirect, request
    from werkzeug.serving import make_server

    token = os.environ.get("PDF2STEP_TOKEN") or secrets.token_urlsafe(24)

    @web.app.before_request
    def need_key():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)                                     # DNS rebinding: another name pointing here
        if request.args.get("key") == token:               # first visit from the desktop app
            r = make_response(redirect(request.path))
            r.set_cookie("pdf2step", token, httponly=True, samesite="Strict")
            return r
        if not secrets.compare_digest(request.cookies.get("pdf2step", ""), token):
            abort(403)

    kill_workers_with_me()
    watch_parent(int(os.environ.get("PDF2STEP_PARENT_PID", "0")))
    srv = make_server("127.0.0.1", int(os.environ.get("PDF2STEP_PORT", "0")), web.app, threaded=True)
    print(f"READY {srv.server_port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    multiprocessing.freeze_support()                       # the worker processes start this same exe
    main()
