#!/usr/bin/env bash
# 在云服务器上执行：bash /opt/qwent/deploy.sh
# 只安装 Qwent 自身依赖与 systemd 服务；不改动已有 8000 端口服务与 OpenClaw QQ 渠道。
set -euo pipefail
DEST=/opt/qwent

echo "==> 1/6 目录"
mkdir -p "$DEST"/{data,models,inbox/delta,inbox/base,logs}

echo "==> 2/6 Python 虚拟环境（复用系统已装的 torch/fastapi，只补 4 个缺包）"
if [ ! -x "$DEST/.venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$DEST/.venv"
fi
"$DEST/.venv/bin/pip" install -q --disable-pip-version-check \
  zstandard==0.19.0 sqlite-vec==0.1.9 websocket-client==1.8.0 transformers==4.46.3

echo "==> 3/6 配置文件"
if [ ! -f "$DEST/backend/config.json" ]; then
  echo "缺少 backend/config.json，请从 config-cloud.example.json 复制后填写" >&2
  exit 1
fi
chmod 600 "$DEST/backend/config.json" 2>/dev/null || true

echo "==> 4/6 依赖自检"
"$DEST/.venv/bin/python" - <<'PY'
import importlib.util, sys
need = ["fastapi", "uvicorn", "requests", "numpy", "zstandard",
        "cryptography", "sqlite_vec", "websocket", "transformers", "torch"]
miss = [m for m in need if importlib.util.find_spec(m) is None]
print("缺失依赖:", miss or "无")
sys.exit(1 if miss else 0)
PY

echo "==> 5/6 干净环境导入自检（这一步验证 Linux 兼容性）"
QWENT_CLOUD=1 QWENT_DATA_DIR="$DEST/data" QWENT_MODELS_DIR="$DEST/models" \
  "$DEST/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$DEST/backend/app')
import main
print('import main OK; IS_CLOUD =', main.IS_CLOUD)
"

echo "==> 6/6 systemd"
cp "$DEST/qwent-cloud.service" /etc/systemd/system/qwent-cloud.service
systemctl daemon-reload
systemctl enable qwent-cloud.service >/dev/null 2>&1 || true

echo
echo "部署准备完成。接下来："
echo "  1. 编辑 $DEST/backend/config.json 填 LLM Key / QQ 凭据 / 加密密钥"
echo "  2. 上传基线：python backend/scripts/cloud_cli.py baseline（在本地）"
echo "  3. 云端恢复：cd $DEST && QWENT_CLOUD=1 .venv/bin/python backend/scripts/cloud_cli.py restore"
echo "  4. 启动服务：systemctl start qwent-cloud"
echo "  5. 健康检查：curl -s http://127.0.0.1:8721/cloud/v1/health"
