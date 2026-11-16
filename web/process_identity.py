"""Read-only process identity; PID existence alone is not enough after a restart."""
import os
from pathlib import Path
import sys
from datetime import datetime


def identity(pid):
    if not isinstance(pid,int) or pid<=0:return None
    if sys.platform=='win32':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return None
        try:
            created,exited,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
            if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel_time),ctypes.byref(user_time)):return None
            kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
            size=wintypes.DWORD(32768);buffer=ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)):return None
            ticks=(created.dwHighDateTime<<32)|created.dwLowDateTime
            return {'pid':pid,'executable':str(Path(buffer.value).resolve()).casefold(),'created':ticks,
                    'created_unix':ticks/10_000_000-11644473600}
        finally:
            kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle)
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return {'pid':pid,'executable':os.readlink(f'/proc/{pid}/exe'),'created':fields[19]}
    except (OSError,IndexError):return None


def worker_alive(worker):
    if not isinstance(worker,dict):return False
    current=identity(worker.get('pid'))
    if not current:return False
    expected=worker.get('identity')
    if expected:
        return all(expected.get(k)==current.get(k) for k in ('pid','executable','created'))
    # Compatibility with older checkpoints: verify start time and Python identity.
    try:
        stamp=datetime.fromisoformat(worker['started_at']).timestamp()
        return 'python' in Path(current['executable']).name.casefold() and abs(current['created_unix']-stamp)<10
    except (KeyError,TypeError,ValueError):return False
