"""Windows ConPTY wrapper using ctypes.

Provides a minimal interface to the Windows Pseudo Console (ConPTY) API
available since Windows 10 1809.  This module is imported lazily by
``ProcessManager`` and degrades gracefully if the API is unavailable
(running on an older Windows version, or not on Windows at all).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Windows API constants
# ---------------------------------------------------------------------------

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x20016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

INVALID_HANDLE_VALUE = -1
HRESULT = ctypes.c_long

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12


class COORD(ctypes.Structure):
    """Win32 console dimensions."""

    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

# ---------------------------------------------------------------------------
# Load kernel32 functions
# ---------------------------------------------------------------------------

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- Pipes ---
CreatePipe = _kernel32.CreatePipe
CreatePipe.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),  # hReadPipe
    ctypes.POINTER(wintypes.HANDLE),  # hWritePipe
    ctypes.c_void_p,  # lpPipeAttributes (SECURITY_ATTRIBUTES)
    wintypes.DWORD,  # nSize
]
CreatePipe.restype = wintypes.BOOL

# --- Pseudo Console ---
CreatePseudoConsole = _kernel32.CreatePseudoConsole
CreatePseudoConsole.argtypes = [
    COORD,  # size
    wintypes.HANDLE,  # hInput
    wintypes.HANDLE,  # hOutput
    wintypes.DWORD,  # dwFlags
    ctypes.POINTER(wintypes.HANDLE),  # phPC
]
CreatePseudoConsole.restype = HRESULT

ClosePseudoConsole = _kernel32.ClosePseudoConsole
ClosePseudoConsole.argtypes = [wintypes.HANDLE]
ClosePseudoConsole.restype = None

ResizePseudoConsole = _kernel32.ResizePseudoConsole
ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
ResizePseudoConsole.restype = HRESULT

# --- Process startup ---
InitializeProcThreadAttributeList = _kernel32.InitializeProcThreadAttributeList
InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,  # lpAttributeList
    wintypes.DWORD,  # dwAttributeCount
    wintypes.DWORD,  # dwFlags
    ctypes.POINTER(ctypes.c_size_t),  # lpSize
]
InitializeProcThreadAttributeList.restype = wintypes.BOOL

UpdateProcThreadAttribute = _kernel32.UpdateProcThreadAttribute
UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,  # lpAttributeList
    wintypes.DWORD,  # dwFlags
    ctypes.c_size_t,  # Attribute
    ctypes.c_void_p,  # lpValue
    ctypes.c_size_t,  # cbSize
    ctypes.c_void_p,  # lpPreviousValue
    ctypes.c_void_p,  # lpReturnSize
]
UpdateProcThreadAttribute.restype = wintypes.BOOL

DeleteProcThreadAttributeList = _kernel32.DeleteProcThreadAttributeList
DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
DeleteProcThreadAttributeList.restype = None

# --- CreateProcess ---
CreateProcessW = _kernel32.CreateProcessW
CreateProcessW.argtypes = [
    wintypes.LPCWSTR,  # lpApplicationName
    wintypes.LPWSTR,  # lpCommandLine
    ctypes.c_void_p,  # lpProcessAttributes
    ctypes.c_void_p,  # lpThreadAttributes
    wintypes.BOOL,  # bInheritHandles
    wintypes.DWORD,  # dwCreationFlags
    ctypes.c_void_p,  # lpEnvironment
    wintypes.LPCWSTR,  # lpCurrentDirectory
    ctypes.c_void_p,  # lpStartupInfo
    ctypes.c_void_p,  # lpProcessInformation
]
CreateProcessW.restype = wintypes.BOOL

# --- Handle management ---
CloseHandle = _kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

# --- Security attributes ---
class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class ConPTYWrapper:
    """Factory for creating Windows ConPTY-based interactive processes."""

    _available: bool | None = None

    @classmethod
    def is_available(cls) -> bool:
        """Return True if the ConPTY API is available on this system."""
        if cls._available is not None:
            return cls._available
        try:
            # Attempt to resolve the function pointer.
            _kernel32.CreatePseudoConsole
            cls._available = True
        except AttributeError:
            cls._available = False
        return cls._available

    @classmethod
    def create_pipe(cls, *, inherit: bool = False) -> tuple[int, int]:
        """Create a Windows pipe, returning (read_handle, write_handle).

        Both handles are returned as raw integer HANDLE values.
        """
        sa = SecurityAttributes()
        sa.nLength = ctypes.sizeof(sa)
        sa.bInheritHandle = inherit
        sa.lpSecurityDescriptor = None

        read_handle = wintypes.HANDLE(0)
        write_handle = wintypes.HANDLE(0)

        if not CreatePipe(
            ctypes.byref(read_handle),
            ctypes.byref(write_handle),
            ctypes.byref(sa),
            0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        if read_handle.value is None or write_handle.value is None:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(read_handle.value), int(write_handle.value)

    @classmethod
    def create(cls, width: int = 80, height: int = 24) -> dict[str, Any]:
        """Create a new pseudo console.

        Returns a dict with keys:
        - ``con_pty_handle``: the pseudo console handle (int)
        - ``pty_input_write``: the write end of the PTY input pipe (int)
        - ``pty_output_read``: the read end of the PTY output pipe (int)

        The caller is responsible for closing all handles.
        """
        # Create the PTY input pipe (host writes → child reads).
        pty_in_read, pty_in_write = cls.create_pipe(inherit=True)
        # Create the PTY output pipe (child writes → host reads).
        pty_out_read, pty_out_write = cls.create_pipe(inherit=True)

        con_pty_handle = wintypes.HANDLE(0)
        hr = CreatePseudoConsole(
            COORD(width, height),
            wintypes.HANDLE(pty_in_read),
            wintypes.HANDLE(pty_out_write),
            0,  # dwFlags
            ctypes.byref(con_pty_handle),
        )

        if hr != 0:
            CloseHandle(wintypes.HANDLE(pty_in_read))
            CloseHandle(wintypes.HANDLE(pty_in_write))
            CloseHandle(wintypes.HANDLE(pty_out_read))
            CloseHandle(wintypes.HANDLE(pty_out_write))
            raise ctypes.WinError(hr)

        return {
            "con_pty_handle": con_pty_handle.value,
            "pty_input_write": pty_in_write,
            "pty_output_read": pty_out_read,
            # These must remain open until CreateProcessW has attached the
            # child, per Microsoft's pseudoconsole startup contract.
            "pty_input_read": pty_in_read,
            "pty_output_write": pty_out_write,
        }

    @classmethod
    def start_process(
        cls,
        con_pty_handle: int,
        command_line: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start a child process attached to the pseudo console.

        Returns a dict with ``process_handle``, ``thread_handle``, and
        ``pid``.
        """
        # Build attribute list.
        attr_size = ctypes.c_size_t(0)
        InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buffer = ctypes.create_string_buffer(attr_size.value)
        if not InitializeProcThreadAttributeList(attr_buffer, 1, 0, ctypes.byref(attr_size)):
            raise ctypes.WinError(ctypes.get_last_error())

        # Attach the pseudo console.
        # HPCON is itself an opaque pointer value. The attribute API expects
        # that value directly (not a pointer to a HANDLE variable).
        attr_value = ctypes.c_void_p(con_pty_handle)
        if not UpdateProcThreadAttribute(
            attr_buffer,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            attr_value,
            ctypes.sizeof(ctypes.c_void_p),
            None,
            None,
        ):
            DeleteProcThreadAttributeList(attr_buffer)
            raise ctypes.WinError(ctypes.get_last_error())

        # Build STARTUPINFOEX.
        si = STARTUPINFOEXW()
        si.cb = ctypes.sizeof(si)
        si.lpAttributeList = ctypes.cast(attr_buffer, ctypes.c_void_p)
        # Prevent a console parent (including pytest/the MCP host) from
        # leaking its redirected standard handles into the child. ConPTY
        # supplies the actual console streams after process attachment.
        si.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        si.hStdInput = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        si.hStdOutput = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        si.hStdError = wintypes.HANDLE(INVALID_HANDLE_VALUE)

        pi = ProcessInformation()

        # Build environment block.
        env_block: Any = None
        env_pointer: Any = None
        if env:
            env_text = "\0".join(f"{key}={value}" for key, value in sorted(env.items()))
            env_block = ctypes.create_unicode_buffer(env_text + "\0")
            env_pointer = ctypes.cast(env_block, ctypes.c_void_p)

        command_buffer = ctypes.create_unicode_buffer(command_line)

        # Create the process.
        if not CreateProcessW(
            None,  # lpApplicationName
            command_buffer,  # lpCommandLine
            None,  # lpProcessAttributes
            None,  # lpThreadAttributes
            False,  # bInheritHandles
            EXTENDED_STARTUPINFO_PRESENT | (0x00000400 if env else 0),  # CREATE_UNICODE_ENVIRONMENT
            env_pointer,  # lpEnvironment
            cwd,  # lpCurrentDirectory
            ctypes.byref(si),  # lpStartupInfo
            ctypes.byref(pi),  # lpProcessInformation
        ):
            DeleteProcThreadAttributeList(attr_buffer)
            raise ctypes.WinError(ctypes.get_last_error())

        DeleteProcThreadAttributeList(attr_buffer)

        # Close the thread handle (we don't need it).
        CloseHandle(pi.hThread)

        return {
            "process_handle": int(pi.hProcess or 0),
            "pid": pi.dwProcessId,
        }

    @staticmethod
    def write_input(pty_input_handle: int, text: str) -> None:
        """Write *text* to the PTY input pipe."""
        data = text.encode("utf-8")
        written = wintypes.DWORD(0)
        if not _kernel32.WriteFile(
            wintypes.HANDLE(pty_input_handle),
            data,
            wintypes.DWORD(len(data)),
            ctypes.byref(written),
            None,
        ):
            log.warning("WriteFile to PTY input failed: last_error=%d", ctypes.get_last_error())

    @staticmethod
    def read_output(pty_output_handle: int, max_bytes: int = 4096) -> bytes:
        """Read one chunk from the PTY output HANDLE."""
        buffer = ctypes.create_string_buffer(max_bytes)
        read = wintypes.DWORD(0)
        if not _kernel32.ReadFile(
            wintypes.HANDLE(pty_output_handle),
            buffer,
            wintypes.DWORD(max_bytes),
            ctypes.byref(read),
            None,
        ):
            error = ctypes.get_last_error()
            if error in {109, 232}:  # broken/no-data pipe
                return b""
            raise ctypes.WinError(error)
        return buffer.raw[: read.value]

    @staticmethod
    def close_input(pty_input_handle: int) -> None:
        """Close the PTY input pipe, sending EOF to the child."""
        CloseHandle(wintypes.HANDLE(pty_input_handle))

    @staticmethod
    def close_handle(handle: int) -> None:
        """Close one raw Win32 HANDLE."""
        CloseHandle(wintypes.HANDLE(handle))

    @staticmethod
    def send_interrupt(pty_input_handle: int) -> None:
        """Send the terminal Ctrl+C control byte to the pseudo console."""
        try:
            ctrl_c = b"\x03"
            written = wintypes.DWORD(0)
            if not _kernel32.WriteFile(
                wintypes.HANDLE(pty_input_handle),
                ctrl_c,
                wintypes.DWORD(len(ctrl_c)),
                ctypes.byref(written),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception as exc:
            log.warning("send_interrupt failed: %s", exc)

    @staticmethod
    def resize(con_pty_handle: int, width: int, height: int) -> None:
        """Resize the pseudo console."""
        result = ResizePseudoConsole(wintypes.HANDLE(con_pty_handle), COORD(width, height))
        if result != 0:
            raise ctypes.WinError(result)

    @staticmethod
    def close(con_pty_handle: int, pty_input_write: int, pty_output_read: int) -> None:
        """Close all handles associated with a pseudo console."""
        ClosePseudoConsole(wintypes.HANDLE(con_pty_handle))
        CloseHandle(wintypes.HANDLE(pty_input_write))
        CloseHandle(wintypes.HANDLE(pty_output_read))
