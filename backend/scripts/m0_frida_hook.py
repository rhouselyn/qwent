# -*- coding: utf-8 -*-
"""M0-① 运行时 hook：用 frida 挂钩微信进程的 Windows CNG 加密接口，
在密钥被使用的瞬间捕获（静态扫描已证明 4.1.15 对密钥做了内存混淆）。

hook 点：
  BCryptGenerateSymmetricKey  — pbSecret 参数即原始 AES 密钥
  BCryptImportKey             — pbInput 指向含密钥的 BLOB（截取前 64 字节）
捕获到的候选写入 data/frida_keys.log，随后用 page1 指纹校验。

用法：python m0_frida_hook.py [--pid 21800] [--seconds 120]
运行期间请在微信里点开几个聊天窗口/等它同步，触发解密路径。
"""
import argparse
import os
import sys
import time

import frida

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                   "data", "frida_keys.log"))

JS = r"""
'use strict';
var seen = 0;
function dump(addr, size, tag) {
    try {
        var bytes = Memory.readByteArray(addr, Math.min(size, 64));
        send({tag: tag}, bytes);
        seen++;
    } catch (e) { }
}
var n = Module.getExportByName('bcrypt.dll', 'BCryptGenerateSymmetricKey');
Interceptor.attach(n, {
    onEnter: function (args) {
        // (hAlg, phKey, pbKeyObject, cbKeyObject, pbSecret, cbSecret, flags)
        var cb = args[5].toInt32();
        if (cb >= 16 && cb <= 64) dump(args[4], cb, 'GenSymKey');
    }
});
var m = Module.getExportByName('bcrypt.dll', 'BCryptImportKey');
Interceptor.attach(m, {
    onEnter: function (args) {
        // (hAlg, phImportKey, hKeyObject, pbKeyObject, cbKeyObject, pbInput, cbInput, ...)
        var cb = args[6].toInt32();
        if (cb >= 24) dump(args[5], Math.min(cb, 64), 'ImportKey');
    }
});
var m2 = Module.getExportByName('bcrypt.dll', 'BCryptImportKeyPair');
Interceptor.attach(m2, {
    onEnter: function (args) {
        var cb = args[6].toInt32();
        if (cb >= 24) dump(args[5], Math.min(cb, 64), 'ImportKeyPair');
    }
});
send({tag: 'ready', hooks: 3});
"""


def on_message(message, data):
    if message.get("type") != "send":
        return
    payload = message["payload"]
    if payload.get("tag") == "ready":
        print(f"[*] frida hooks 就绪: {payload}")
        return
    tag = payload.get("tag")
    if data:
        hexs = data.hex()
        with open(OUT, "a") as f:
            f.write(f"{tag} {hexs}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=21800)
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w").close()
    session = frida.attach(args.pid)
    script = session.create_script(JS)
    script.on("message", on_message)
    script.load()
    print(f"[*] 已注入 pid={args.pid}，监听 {args.seconds}s —— "
          f"请在微信里点开几个聊天窗口、上下滚动消息，触发解密")
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(2)
    session.detach()
    n = sum(1 for _ in open(OUT))
    print(f"[*] 捕获候选 {n} 条，保存于 {OUT}")
    # 立即用 page1 指纹校验
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.join(here, "..", "app"))
    from m0lib import check_raw_key, load_page1
    from paths import ACCOUNT_DIR
    page1 = load_page1(os.path.join(str(ACCOUNT_DIR),
                                    "db_storage", "message", "message_0.db"))
    hit = False
    for line in open(OUT):
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            blob = bytes.fromhex(parts[1])
        except ValueError:
            continue
        for i in range(0, len(blob) - 32):
            key = blob[i:i + 32]
            r = next((rr for rr in (80, 48, 16) if check_raw_key(key, page1, rr)), None)
            if r:
                print(f"[+] 命中! {key.hex()} (reserve={r}, src={parts[0]})")
                with open(os.path.join(os.path.dirname(OUT), "keys.json"), "w") as f:
                    import json
                    json.dump({"key_hex": key.hex(), "reserve": r, "kind": "frida-cng"}, f)
                hit = True
    if not hit:
        print("[-] 本次会话未捕获到有效密钥。可加长 --seconds、在微信中多切换几个会话后重试")


if __name__ == "__main__":
    main()
