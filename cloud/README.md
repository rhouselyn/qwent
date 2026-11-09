# Qwent 云端最小运行单元

云端只跑：FastAPI 问答服务（`/v1/chat/completions`）、QQ 官方机器人网关、增量应用循环。
**不含**前端、桌面壳、微信/QQ 解密、OCR/ASR 模型、朋友圈 WASM。前端怎么改都不影响这里。

## 目录

```
/opt/qwent/
  backend/app/*.py        与本地同一份源码（由 build_cloud_bundle.py 拷贝）
  backend/config.json     云端配置
  cloud_server.py         uvicorn 入口，QWENT_CLOUD=1，只监听 127.0.0.1:8721
  data/knowledge.db       基线恢复出的只读知识库副本
  models/bge-small-zh/    向量模型（与本地同一份，保住语义检索）
  inbox/                  SCP 上传的加密增量包与基线分片
  .venv/                  --system-site-packages，只补 4 个缺包
```

## 部署

```bash
# 本地
python backend/scripts/build_cloud_bundle.py
scp -i C:/Users/14815/.ssh/tencent_key dist/qwent-cloud.zip root@152.136.96.212:/opt/
# 服务器
cd /opt && unzip -o qwent-cloud.zip -d qwent && bash /opt/qwent/deploy.sh
```

编辑 `/opt/qwent/backend/config.json`，填：`llm.api_key`、`agent_api.api_key`（**与本地同一个值**）、
`qqbot.app_id/app_secret`（`1906230635`，与服务器上已有的 `1903204947` 不是同一账号，不冲突）、
`cloud.encrypt_key`（**从本地 `backend/config.json` 复制**，两端必须完全一致）。

> 加密密钥由本地首次开启「云端分流」时自动生成（32 字节 base64，AES-GCM），
> 设置页不显示、也不要求填写。它只用于加密增量包与基线分片，不是登录密码、不是 LLM API Key。
> 换服务器时把这把密钥复制过来即可；两端不一致云端会解包失败。
> 也可用环境变量 `QWENT_CLOUD_KEY` 覆盖（两端都要设）。

```bash
systemctl start qwent-cloud
curl -s http://127.0.0.1:8721/cloud/v1/health
```

## 数据同步

本地：`python backend/scripts/cloud_cli.py triggers-install` → `baseline` → 之后每 10 分钟自动 `sync`，
或设置页点「立即同步」。传输走 SSH/SFTP 到 `/opt/qwent/inbox`，两端都不需要公网入站端口。

云端：服务启动后自动每 30 秒轮询 inbox，按序号幂等应用增量包；会话表
（`ai_qa/ai_session/qq_message/qq_conv`）由云端机器人独立写入，不参与同步。

本地常驻循环随 Qwent 服务启动，先等满一个 `interval_min` 周期再发第一包；
设置页「立即增量同步」按钮走同一个 `sync_now()`，无需等定时，随时可手动触发。

## 换一台电脑时

服务器不绑定电脑，微信凭据也不绑定电脑。**只要服务器上的凭据没失效，换新电脑什么都不用做**：
服务器上的 QQ / 微信机器人、问答服务、增量应用循环继续常驻，本地关机也不受影响。

新电脑上要重新接上「本地 → 云端」的同步链路，需要三样东西：

1. 项目代码 + `backend/config.json`（含 `cloud` 段、自动生成的 `cloud.encrypt_key`、
   `agent_api.api_key`、`qqbot` 凭据，与服务器保持一致）与 `~/.ssh/tencent_key`。
   密钥随配置文件一起迁移，不需要重新生成，也无需在设置页填写。
2. 本地知识库 `data/knowledge.db`（重新解密导入微信 / QQ 数据）与 `models/bge-small-zh`。
3. **重做一次全量基线**：设置页「云端分流 → 生成全量基线」，再到服务器执行
   `QWENT_CLOUD=1 .venv/bin/python backend/scripts/cloud_cli.py restore`。

第 3 步是硬要求：同步水位（`epoch` / `last_sent_seq` / `last_sent_lsn`）存在
`data/cloud_state.json` 和 `knowledge.db` 的 `_cloud_sync_state` 表里，新电脑从 0 开始会
与服务器对不上。`sync_now()` 带水位校验，遇到这种情况会明确报
「本地没有基线水位 / epoch 与云端不一致 / 序号接不上」，**不会假装同步成功**。
重做基线生成新 `epoch`，云端 `restore` 把 `applied_seq` 归零并保留会话表，
之后增量从 `seq=1` 重新接上。

> 若只是同一台电脑换目录 / 重装，且 `data/cloud_state.json` 与 `knowledge.db`
> 一起带过去，水位能续上，不必重做基线。

QQ 机器人凭据在 `config.json` 里，复制配置即可，**不需要扫码**。
微信凭据见下一节：只有凭据失效才需要重新扫码。

## 微信通道：不扫码的凭据迁移

官方渠道 `openclaw-weixin` **只有扫码登录这一种登录方式**，`pairing` 是批准联系人的
访问控制、不是登录。但扫码后凭据以 token 形式落盘，可以迁到服务器，因此无需再扫一次：

```bash
# 本地（Windows，Git Bash）
scp -i C:/Users/14815/.ssh/tencent_key -r \
  "C:/Users/14815/.openclaw/openclaw-weixin" \
  root@152.136.96.212:/root/.openclaw/openclaw-weixin

# 服务器：装/更新插件。版本很关键 —— 2.4.8 引用了 core 2026.10.9 未导出的
# openclaw/plugin-sdk/infra-runtime，会加载失败；2.4.10 已去掉该依赖。
openclaw plugins install "@tencent-weixin/openclaw-weixin@2.4.10" \
  --accept-capabilities --acknowledge-install-policy-warning
# 若已装过旧版，改用：openclaw plugins update "@tencent-weixin/openclaw-weixin@2.4.10"
openclaw plugins list | grep -i weixin

KEY=$(python3 -c 'import json;print(json.load(open("/opt/qwent/backend/config.json"))["agent_api"]["api_key"])')
python3 /opt/qwent/merge_openclaw.py --api-key "$KEY"                 # 先看 dry-run
python3 /opt/qwent/merge_openclaw.py --apply --api-key "$KEY"         # 写入并自动备份

# 微信消息必须显式路由到 qwent，否则会落到默认的 main/deepseek
openclaw agents bind --agent qwent --bind "openclaw-weixin:<accounts.json 里的账号 id>"
openclaw gateway restart
openclaw config validate
openclaw agents list                    # qwent 的 Routing rules 应为 1
openclaw channels status --probe        # 应看到 openclaw-weixin <账号>: running
```

三个已踩过的坑，重新部署时务必照做：

1. **多 agent 归属**：新增 `qwent` 后必须声明 `agents.ownership="explicit"`
   （模板 `openclaw.json.example` 已带）。否则 `openclaw config validate` 直接失败，
   Gateway 起不来，连服务器上原有的 QQ Bot 也会一起下线。
2. **插件版本**：core `2026.10.9` 配 `2.4.10`；`2.4.8` 会报
   `ERR_PACKAGE_PATH_NOT_EXPORTED: ./plugin-sdk/infra-runtime`。
3. **渠道绑定**：`agents.ownership="explicit"` 下没有隐式路由，不执行 `agents bind`
   微信消息不会进 Qwent 知识库。

> 同一微信账号凭据**不能两端同时在线**：云端渠道起来后必须停掉本机 OpenClaw Gateway。
> 本地把 `cloud.enabled` 打开并重启 Qwent 即会自动停本机网关、关本机 qqbot；
> 本地 Qwent 网页/桌面问答始终走本机实时库，不受影响。
### 凭据失效 / 新电脑刚扫码

只在服务器凭据失效（或换了微信账号）时才需要重新扫码，而且**扫码在本地做**：

```bash
# 1. 本地扫码登录
openclaw channels login --channel openclaw-weixin
# 2. 先看会传什么，再真正推送
python cloud/push_weixin_creds.py
python cloud/push_weixin_creds.py --apply
```

脚本把 `~/.openclaw/openclaw-weixin/` 打包上传，先把服务器原目录改名为
`openclaw-weixin.bak-<时间戳>`（可回滚），再覆盖、重启 Gateway、探测渠道状态，
全程不打印任何 token。推送后保持本机微信通道关闭，否则同一账号两端互踢。
账号 ID 变化时需重新执行一次 `openclaw agents bind --agent qwent --bind ...`。

> 上面的 `scp -r` 是手工做法，日常用脚本即可；脚本额外做了备份、清理
> `-wal/-shm`、重启与探测。

`merge_openclaw.py` 默认 dry-run，只新增插件/provider/qwent agent，
**不改** `agents.defaults.model.primary`，服务器上已有的 QQ Bot（老仙女）行为不变。

## 回滚

```bash
systemctl stop qwent-cloud
# 本地恢复机器人：设置页关掉云端分流，或 cloud_cli.py off
python backend/scripts/cloud_cli.py triggers-drop
# 服务器如需彻底清理：rm -rf /opt/qwent /root/.openclaw/openclaw-weixin
```
