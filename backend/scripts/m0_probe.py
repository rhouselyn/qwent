# -*- coding: utf-8 -*-
"""诊断：密钥在内存中的真实形态。
扫描所有 Weixin.exe 进程，检测：
  1) ASCII x'<hex>' 模式
  2) UTF-16LE x'<hex>' 模式
  3) 已知盐值 hex 字符串（ASCII/UTF-16）出现的位置及上下文
"""
import ctypes
import ctypes.wintypes as wt
import re
import sys

MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


k32 = ctypes.windll.kernel32


def regions_of(h):
    regs, addr = [], 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_uint64(addr),
                              ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 1024**3:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return b""


def main():
    salt_hex = sys.argv[1] if len(sys.argv) > 1 else "47166927b32af9ad8fd9bf48cfa8c57b"
    pids = []
    import subprocess
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    for line in r.stdout.strip().splitlines():
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pids.append((int(p[1]), p[4].replace(",", "").replace(" K", "").strip()))
    print(f"进程: {pids}")
    for pid, mem in pids:
        h = k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            print(f"pid {pid}: OpenProcess 失败")
            continue
        regs = regions_of(h)
        total = sum(s for _, s in regs)
        ascii_hits = utf16_hits = salt_hits = 0
        samples = []
        salt_re = re.compile(salt_hex.encode())
        salt_re16 = re.compile(salt_hex.encode("utf-16-le"))
        x_re = re.compile(rb"x'([0-9a-fA-F]{64,192})'")
        x_re16 = re.compile("x'".encode("utf-16-le") + b"([0-9a-fA-F]{64,192})")
        for base, size in regs:
            data = read_mem(h, base, size)
            if not data:
                continue
            for m in x_re.finditer(data):
                ascii_hits += 1
                if len(samples) < 3:
                    samples.append(("ascii-x", hex(base + m.start()), m.group()[:120]))
            for m in x_re16.finditer(data):
                utf16_hits += 1
                if len(samples) < 6:
                    samples.append(("utf16-x", hex(base + m.start()), m.group()[:120]))
            for m in salt_re.finditer(data):
                salt_hits += 1
                ctx = data[max(0, m.start()-96): m.end()+48]
                if len(samples) < 9:
                    samples.append(("salt-ascii", hex(base + m.start()), ctx))
            for m in salt_re16.finditer(data):
                salt_hits += 1
                ctx = data[max(0, m.start()-96): m.end()+48]
                if len(samples) < 12:
                    samples.append(("salt-utf16", hex(base + m.start()), ctx))
        print(f"\npid {pid} ({int(mem)//1024}MB): 区域 {len(regs)} 个, {total//1048576}MB | "
              f"ascii-x:{ascii_hits} utf16-x:{utf16_hits} salt:{salt_hits}")
        for s in samples:
            print("   ", s[0], s[1], s[2][:110])
        k32.CloseHandle(ctypes.c_void_p(h))


if __name__ == "__main__":
    main()
