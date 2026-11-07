"""Windows 外部只读访问：仅查询/读取，不写入、不附加、不提权。"""
import ctypes
from ctypes import wintypes
import struct
import time

READ_ACCESS = 0x0010 | 0x0400
MAX_TOTAL_BYTES = 1024 * 1024
MAX_SINGLE_READ = 65536
MAX_SECONDS = 45


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260),
    ]


class ReadOnlyProcess:
    def __init__(self, pid, max_bytes=MAX_TOTAL_BYTES, max_seconds=MAX_SECONDS):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("要求 64 位 Python")
        self.max_bytes = max_bytes
        self.max_seconds = max_seconds
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32.CloseHandle.restype = wintypes.BOOL
        self.k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.k32.ReadProcessMemory.restype = wintypes.BOOL
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        self.k32.Module32FirstW.restype = wintypes.BOOL
        self.k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        self.k32.Module32NextW.restype = wintypes.BOOL
        self.pid = pid
        self.total_bytes = 0
        self.read_count = 0
        self.start = time.monotonic()
        self.handle = self.k32.OpenProcess(READ_ACCESS, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None

    def image_path(self):
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not self.k32.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.value

    def module(self, expected_name):
        snapshot = self.k32.CreateToolhelp32Snapshot(0x08 | 0x10, self.pid)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = self.k32.Module32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szModule.casefold() == expected_name.casefold():
                    return {"base": ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value, "size": entry.modBaseSize, "path": entry.szExePath}
                ok = self.k32.Module32NextW(snapshot, ctypes.byref(entry))
            raise ValueError("目标模块未加载")
        finally:
            self.k32.CloseHandle(snapshot)

    def read(self, address, size):
        if not isinstance(address, int) or address < 65536 or address >= 0x0000800000000000:
            raise ValueError("无效用户空间地址")
        if size <= 0 or size > MAX_SINGLE_READ or self.total_bytes + size > self.max_bytes:
            raise ValueError("达到有界读取额度")
        if time.monotonic() - self.start > self.max_seconds:
            raise TimeoutError("达到只读时间上限")
        buffer = ctypes.create_string_buffer(size)
        copied = ctypes.c_size_t()
        if not self.k32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(copied)):
            raise ctypes.WinError(ctypes.get_last_error())
        if copied.value != size:
            raise ValueError("读取不完整，停止")
        self.total_bytes += size
        self.read_count += 1
        return buffer.raw

    def u64(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def i32(self, address):
        return struct.unpack("<i", self.read(address, 4))[0]

    def u16(self, address):
        return struct.unpack("<H", self.read(address, 2))[0]

    def u32(self, address):
        return struct.unpack("<I", self.read(address, 4))[0]

    def field_count(self, klass):
        return self.u16(klass + 0x120)

    def element_size(self, klass):
        return self.u32(klass + 0x100)

    def array_info(self, address):
        klass = self.u64(address)
        element = self.u64(klass + 0x40)
        return {"klass": klass, "element_class": element, "element_name": self.class_name(element), "max_length": self.u64(address + 0x18), "data": address + 0x20}

    def cstring(self, address, limit=160):
        data = bytearray()
        while len(data) < limit:
            block = self.read(address + len(data), min(16, limit - len(data)))
            end = block.find(b"\x00")
            if end >= 0:
                data.extend(block[:end])
                return data.decode("utf-8", errors="strict")
            data.extend(block)
        raise ValueError("类型/字段名超出长度限制")

    def class_name(self, klass):
        name = self.cstring(self.u64(klass + 0x10))
        namespace = self.cstring(self.u64(klass + 0x18))
        return namespace + "." + name if namespace else name

    def fields(self, klass, count):
        if count < 0 or count > 256:
            raise ValueError("字段数超出范围")
        pointer = self.u64(klass + 0x80)
        if not count:
            return []
        raw = self.read(pointer, count * 32)
        result = []
        for index in range(count):
            name_pointer, type_pointer, parent, offset, token = struct.unpack_from("<QQQiI", raw, index * 32)
            if parent != klass:
                raise ValueError("运行时 FieldInfo 所属类不匹配")
            result.append({"name": self.cstring(name_pointer), "offset": offset, "token": token})
        return result
