// WxIsaac64 keystream 常驻守护进程
// 协议：stdin 每行 "key size"（可多行）；stdout 回一行 "OK <len>" 后紧跟原始 keystream 字节。
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const here = __dirname;
const jsPath = path.join(here, "wasm_video_decode.js");
const wasmPath = path.join(here, "wasm_video_decode.wasm");

let captured = null;
const mockGlobal = {
  Buffer, Uint8Array, Int8Array, Uint16Array, Int16Array,
  Uint32Array, Int32Array, Float32Array, Float64Array,
  BigInt64Array, BigUint64Array, Array, Object, Function,
  String, Number, Boolean, Error, Promise,
  require, process, setTimeout, clearTimeout, setInterval, clearInterval,
};
mockGlobal.Module = {
  wasmBinary: fs.readFileSync(wasmPath),
  print: () => {},
  printErr: (t) => console.error("[wasm]", t),
  onRuntimeInitialized: () => { mockGlobal.__ready = true; },
};
mockGlobal.wasm_isaac_generate = (ptr, size) => {
  captured = Buffer.from(new Uint8Array(mockGlobal.Module.HEAPU8.buffer, ptr, size));
};
mockGlobal.self = mockGlobal;
mockGlobal.self.location = { href: jsPath };
mockGlobal.WorkerGlobalScope = function () {};
mockGlobal.VTS_WASM_URL = "file://" + wasmPath;

const ctx = vm.createContext(mockGlobal);
new vm.Script(fs.readFileSync(jsPath, "utf8"), { filename: jsPath }).runInContext(ctx);

let Ctor = null;
const wait = setInterval(() => {
  Ctor = mockGlobal.Module.WxIsaac64 ||
    (mockGlobal.Module.asm && mockGlobal.Module.asm.WxIsaac64);
  if (Ctor) { clearInterval(wait); pump(); }
}, 10);
setTimeout(() => { console.error("init timeout"); process.exit(1); }, 20000);

let buf = "";
function pump() {
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      const sp = line.lastIndexOf(" ");
      const key = line.slice(0, sp);
      const size = parseInt(line.slice(sp + 1), 10);
      try {
        captured = null;
        const isaac = new Ctor(String(key));
        isaac.generate(size);
        if (isaac.delete) isaac.delete();
        if (!captured) throw new Error("no keystream");
        process.stdout.write(`OK ${captured.length}\n`);
        process.stdout.write(captured);
      } catch (e) {
        process.stdout.write(`ERR ${String(e).slice(0, 120)}\n`);
      }
    }
  });
}
