# 微信知识助手

把本机微信 4.0 的全部聊天记录、收发文件、图片、语音、链接变成**本地个人知识库**,提供语义搜索与带引用溯源的 RAG 问答。

- 只读本地数据,不向微信发送任何消息,零封号风险
- 架构:数据采集(解密微信本地 SQLCipher 库)→ ETL(消息归一化 + 富媒体解析)→ SQLite + 全文 + 向量检索 → RAG 问答 → Web UI
- **开箱即用**:微信账号目录自动发现,模型权重自动下载(国内镜像),首次用到才触发

## 目录结构

```
backend/
  app/            FastAPI 后端(agent、RAG、媒体解析、流式问答、账号发现 paths.py)
  frontend/       Web 前端(原生 JS)
  scripts/        数据管线(解密 / 导入 / 建向量 / 搜索 / 密钥扫描 / download_models.py)
  server.py       服务入口(默认端口 8721)
  config.json     本机配置(含 API key,不入库;模板见 config.example.json)
data/             解密数据与 knowledge.db(私密,不入库)
models/           本地模型权重(不入库,首次运行自动下载)
refs/             第三方依赖 ChatTrace(不入库,可自动 clone)
需求文档.md / 项目文档.md
```

## 快速开始

环境要求:Windows 10/11、Python 3.11+、微信 4.x(本机登录过、本地有聊天记录)。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置:复制模板,填入 DeepSeek API key,其余全部留空即可
copy backend\config.example.json backend\config.json

# 3. (可选)一键预下载模型与 ChatTrace;不跑也行,首次用到会自动下载
python backend/scripts/download_models.py           # 全部
python backend/scripts/download_models.py --check   # 查看状态

# 4. 数据管线(各脚本 --help 查看参数)
python backend/scripts/m0_decrypt.py        # 解密微信本地库 → data/
python backend/scripts/m0_import.py         # 解析消息 → data/knowledge.db
python backend/scripts/build_vectors.py     # 构建向量索引

# 5. 启动(或双击 启动助手.bat)
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
| `models/bge-small-zh` | 中文向量嵌入 | ~95MB | BAAI/bge-small-zh-v1.5,优先 hf-mirror.com 镜像 |
| `models/para-zh` | 语音消息/视频转文字 | ~78MB | sherpa-onnx 官方 releases(paraformer-zh-small) |
| `refs/ChatTrace` | 图片/.dat 媒体解码 | - | [qiaodogbear/ChatTrace](https://github.com/qiaodogbear/ChatTrace)(MIT),自动 `git clone` |

缺哪个就下哪个;向量模型在首次检索时自动下载,语音模型在首次转写时自动下载。也可用 `download_models.py --bge / --asr / --chattrace` 单独控制。

详细技术方案见 [项目文档.md](./项目文档.md),需求背景见 [需求文档.md](./需求文档.md)。

## 安全与隐私

- `data/` 内含**解密后的聊天记录**与数据库密钥(`all_keys.json`),已在 `.gitignore` 中排除,**严禁提交或分享**
- `backend/config.json` 含真实 API key,不入库;仓库只保留 `config.example.json` 模板
- `models/`(模型权重)与 `refs/`(第三方仓库)同样不入库

## 路线图

- ✅ 一期:全量历史消息解密入库、语义/全文搜索、带引用 RAG 问答、语音/图片/链接解析
- ⏳ 二期:评估微信内小号查询入口(参考 wechaty),发送消息、防撤回、朋友圈均不做
