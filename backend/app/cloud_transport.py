# -*- coding: utf-8 -*-
"""增量包传输后端。

默认用 SSH 直传到云服务器（复用已验证可用的 ssh 密钥与命令，无需 paramiko、
无需对象存储凭据、无需公网入站端口）：
  本地侧 transport=scp  → ssh 写 /opt/qwent/inbox
  云端侧 transport=file → 直接读同一个 inbox 目录
transport=file 也用于本地端到端联调。cos/s3 预留接口，当前不实现。
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import cloud_conf


class FileTransport:
    """本地目录后端（云端读 inbox、以及本地联调）。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, name: str) -> Path:
        p = (self.root / name).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"非法路径：{name}")
        return p

    def put_bytes(self, name: str, data: bytes) -> None:
        p = self._p(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def put_file(self, local: Path, name: str) -> None:
        p = self._p(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        shutil.copy2(local, tmp)
        tmp.replace(p)

    def read_bytes(self, name: str) -> bytes:
        return self._p(name).read_bytes()

    def read_text(self, name: str) -> str:
        return self._p(name).read_text(encoding="utf-8")

    def list(self, prefix: str = "") -> list[str]:
        base = self._p(prefix) if prefix else self.root
        if not base.exists():
            return []
        return sorted(str(p.relative_to(self.root)).replace("\\", "/")
                      for p in base.rglob("*") if p.is_file()
                      and not p.name.endswith(".tmp"))

    def exists(self, name: str) -> bool:
        return self._p(name).exists()

    def size(self, name: str) -> int:
        p = self._p(name)
        return p.stat().st_size if p.exists() else -1

    def test(self) -> dict:
        probe = "_probe.json"
        self.put_bytes(probe, b"ok")
        ok = self.read_bytes(probe) == b"ok"
        self._p(probe).unlink(missing_ok=True)
        return {"ok": ok, "backend": "file", "root": str(self.root)}


class SftpTransport:
    """本地 → 云服务器：走系统 ssh 命令，先写 .tmp 再 mv 保证原子。

    不用 paramiko：本机 2.8.x 只发 SHA-1 ssh-rsa，而新 OpenSSH 服务端已禁用，
    显式 pkey 也认证失败；系统 ssh 用 rsa-sha2-256 可正常连接。
    """

    def __init__(self, c: dict):
        self.host = str(c.get("endpoint") or "").strip()
        self.user = str(c.get("scp_user") or "root")
        self.port = int(c.get("scp_port") or 22)
        self.key = str(c.get("scp_key") or "").strip()
        self.remote_dir = str(c.get("scp_dir") or "/opt/qwent/inbox").rstrip("/")
        self._ssh = shutil.which("ssh") or "ssh"
        self._scp = shutil.which("scp") or "scp"

    def _base(self) -> list[str]:
        cmd = [self._ssh, "-p", str(self.port),
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if self.key:
            cmd += ["-i", self.key]
        return cmd + [f"{self.user}@{self.host}"]

    def _run(self, remote_cmd: str, data: bytes | None = None,
             check: bool = True) -> bytes:
        env = dict(os.environ)
        p = subprocess.run(self._base() + [remote_cmd], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=900)
        if check and p.returncode:
            raise RuntimeError(
                f"ssh 失败({p.returncode}): {p.stderr.decode('utf-8', 'ignore')[:300]}")
        return p.stdout

    def _rp(self, name: str) -> str:
        p = posixpath.normpath(posixpath.join(self.remote_dir, name))
        if not p.startswith(self.remote_dir + "/"):
            raise ValueError(f"非法路径：{name}")
        return p

    def _write(self, name: str, data: bytes) -> None:
        remote = self._rp(name)
        q, parent = shlex.quote(remote), shlex.quote(posixpath.dirname(remote))
        self._run(f"mkdir -p {parent} && cat > {q}.tmp && mv {q}.tmp {q}", data)

    def put_bytes(self, name: str, data: bytes) -> None:
        self._write(name, data)

    @staticmethod
    def _msys(path) -> str:
        """Windows 绝对路径转 Git Bash 形式，避免 scp 把 C: 当成远端主机。"""
        s = str(Path(path).resolve()).replace("\\", "/")
        if len(s) > 2 and s[1] == ":":
            return "/" + s[0].lower() + s[2:]
        return s

    def put_file(self, local: Path, name: str) -> None:
        """大分片用 scp 传（比 ssh stdin 管道稳），写 .tmp 后原子改名，失败重试。"""
        remote = self._rp(name)
        q = shlex.quote(remote)
        cmd = [self._scp, "-P", str(self.port),
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if self.key:
            cmd += ["-i", self.key]
        cmd += [self._msys(local), f"{self.user}@{self.host}:{q}.tmp"]
        last = ""
        for attempt in range(3):
            p = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=900)
            if p.returncode == 0:
                self._run(f"mv {q}.tmp {q}")
                return
            last = p.stderr.decode("utf-8", "ignore")[-250:]
            time.sleep(2 + attempt * 3)
        raise RuntimeError(f"scp 上传失败（重试 3 次）：{last}")

    def size(self, name: str) -> int:
        out = self._run(f"stat -c %s {shlex.quote(self._rp(name))} 2>/dev/null || echo -1",
                        check=False)
        try:
            return int(out.strip())
        except ValueError:
            return -1

    def read_bytes(self, name: str) -> bytes:
        return self._run(f"cat {shlex.quote(self._rp(name))}")

    def read_text(self, name: str) -> str:
        return self.read_bytes(name).decode("utf-8")

    def list(self, prefix: str = "") -> list[str]:
        base = posixpath.join(self.remote_dir, prefix) if prefix else self.remote_dir
        out = self._run(
            f"cd {shlex.quote(base)} 2>/dev/null && "
            "find . -type f ! -name '*.tmp' | sed 's|^\\./||' | sort",
            check=False).decode("utf-8", "ignore")
        rel = prefix.rstrip("/")
        return sorted(f"{rel}/{x}" if rel else x
                      for x in (line.strip() for line in out.splitlines()) if x)

    def exists(self, name: str) -> bool:
        return self._run(
            f"test -f {shlex.quote(self._rp(name))} && echo 1",
            check=False).strip() == b"1"

    def test(self) -> dict:
        self.put_bytes("_probe.json", b"ok")
        ok = self.read_bytes("_probe.json") == b"ok"
        self._run(f"rm -f {shlex.quote(self._rp('_probe.json'))}", check=False)
        return {"ok": ok, "backend": "ssh",
                "target": f"{self.user}@{self.host}:{self.remote_dir}"}

    def close(self):
        pass


def get_transport(conf: dict | None = None):
    c = cloud_conf.cloud_conf(conf)
    kind = str(c.get("transport") or "file").lower()
    if kind == "file":
        root = c.get("endpoint") or (cloud_conf.data_dir() / "cloud_inbox")
        return FileTransport(root)
    if kind in ("scp", "sftp", "ssh"):
        return SftpTransport(c)
    raise ValueError(f"不支持的传输后端：{kind}（可用 file / scp）")


def read_json(tr, name: str) -> dict:
    return json.loads(tr.read_text(name))


def write_json(tr, name: str, obj: dict) -> None:
    tr.put_bytes(name, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
