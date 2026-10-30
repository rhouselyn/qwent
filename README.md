# 微信知识助手

把本机微信 4.0 的全部聊天记录、收发文件、图片、语音、链接变成**本地个人知识库**,提供语义搜索与带引用溯源的 RAG 问答。

- 只读本地数据,不向微信发送任何消息,零封号风险
- 架构:数据采集(解密微信本地 SQLCipher 库)→ ETL(消息归一化 + 富媒体解析)→ SQLite + 全文 + 向量检索 → RAG 问答 → Web UI
- **开箱即用**:ChatTrace 已内置在仓库中,微信账号目录自动发现,模型权重自动下载(国内镜像),`python backend/scripts/doctor.py` 一条命令验证就绪

## 目录结构

```
backend/
  app/            FastAPI 后端(agent、RAG、媒体解析、流式问答、账号发现 paths.py)
  frontend/       Web 前端(原生 JS)
  scripts/        数据管线与工具(解密 / 导入 / 建向量 / doctor.py / backup_data.py)
  server.py       服务入口(默认端口 8721)
  config.json     本机配置(含 API key,不入库;模板见 config.example.json)
data/             解密数据与 knowledge.db(私密,不入库,见下方"一比一迁移")
models/           本地模型权重(不入库,自动下载)
refs/ChatTrace    第三方解密/媒体组件 —— 已内置(MIT,来源见其 VENDORED.md)
需求文档.md / 项目文档.md
```

## 克隆后你会得到什么

| 内容 | 是否随仓库 | 怎么就位 |
|---|---|---|
| 全部代码、前端、文档、脚本 | ✅ 随 git | clone 即有 |
| `refs/ChatTrace`(解密/媒体组件) | ✅ **已内置** | clone 即有,无需任何 clone 步骤 |
| `models/` 模型权重(~170MB 必需部分) | ❌ 不入 git | **自动下载**(hf-mirror 镜像),或 `download_models.py` 手动 |
| `data/` 知识库(你的聊天记录) | ❌ 永不入 git | 二选一:① `backup_data.py` 恢复备份(一比一)② 从本机微信重新解密导入 |

`data/` 是解密后的**私人聊天记录**(knowledge.db 3.7GB,超 GitHub 100MB 单文件限制),按设计不进 git;
换电脑想要和现在完全一致,用 [`backup_data.py`](#换电脑一比一迁移) 打包恢复即可。

## 快速开始

环境要求:Windows 10/11、Python 3.11+、微信 4.x(本机登录过、本地有聊天记录)。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置:复制模板,填入 DeepSeek API key,其余全部留空即可
copy backend\config.example.json backend\config.json

# 3. (可选)预下载模型;不跑也行,首次用到会自动下载
python backend/scripts/download_models.py

# 4. 数据管线(新环境且无 data 备份时才需要;各脚本 --help 查看参数)
python backend/scripts/m0_decrypt.py        # 解密微信本地库 → data/
python backend/scripts/m0_import.py         # 解析消息 → data/knowledge.db
python backend/scripts/build_vectors.py     # 构建向量索引

# 5. 自检 + 启动(或双击 启动助手.bat)
python backend/scripts/doctor.py
python backend/server.py 8721
# 浏览器访问 http://127.0.0.1:8721
```

### 微信账号目录自动发现

启动时按以下优先级定位账号目录(即微信 4.x 的 `xwechat_files\wxid_xxx`):

1. `backend/config.json` 的 `account.account_dir`(手动指定,多账号推荐)
2. 环境变量 `WECHAT_ACCOUNT_DIR`
3. 自动扫描 `%USERPROFILE%\xwechat_files\`,取最近使用的账号(多账号时打印全部)

自检:`python backend/app/paths.py` 会打印识别到的目录。

### 模型自动下载

| 模型 | 用途 | 大小 | 来源 |
|---|---|---|---|
| `models/bge-small-zh` | 中文向量嵌入 | ~91MB | BAAI/bge-small-zh-v1.5,优先 hf-mirror.com 镜像 |
| `models/para-zh` | 语音消息/视频转文字 | ~78MB | sherpa-onnx 官方 releases(paraformer-zh-small) |

缺哪个就下哪个;向量模型在首次检索时自动下载,语音模型在首次转写时自动下载。也可用 `download_models.py --bge / --asr` 单独控制。

## 换电脑 / 一比一迁移

`data/` 不随 git 走,想在新机器/新目录**直接可用**(聊天记录、向量索引、媒体缓存全部原样):

```bash
# 旧机器:打包(含 knowledge.db 快照 + 库密钥 all_keys.json + 媒体缓存;服务运行中也可备份)
python backend/scripts/backup_data.py D:\backup\wechat_data.zip

# 新机器:clone + pip install + 配置 key 后,恢复备份即可
python backend/scripts/backup_data.py --restore D:\backup\wechat_data.zip
python backend/scripts/doctor.py      # 验证全部 ✓ 后直接启动
```

没有备份也可以在新机器从头建库(需该机微信登录过且有聊天记录):走「快速开始」第 4 步解密管线;
抓取数据库密钥的步骤见 [项目文档.md](./项目文档.md)(ChatTrace keyagent,密钥 24h 有效)。

详细技术方案见 [项目文档.md](./项目文档.md),需求背景见 [需求文档.md](./需求文档.md)。

## 安全与隐私

- `data/` 内含**解密后的聊天记录**与数据库密钥(`all_keys.json`),已在 `.gitignore` 中排除,**严禁提交或分享**
- `backend/config.json` 含真实 API key,不入库;仓库只保留 `config.example.json` 模板
- `models/`(模型权重)不入库;`refs/` 下仅内置运行时必需的 ChatTrace,其余本地研究资料不入库

## 路线图

- ✅ 一期:全量历史消息解密入库、语义/全文搜索、带引用 RAG 问答、语音/图片/链接解析
- ⏳ 二期:评估微信内小号查询入口(参考 wechaty),发送消息、防撤回、朋友圈均不做
