# 微信知识助手

把本机微信 4.0 的全部聊天记录、收发文件、图片、语音、链接变成**本地个人知识库**,提供语义搜索与带引用溯源的 RAG 问答。

- 只读本地数据,不向微信发送任何消息,零封号风险
- 架构:数据采集(解密微信本地 SQLCipher 库)→ ETL(消息归一化 + 富媒体解析)→ SQLite + 全文 + 向量检索 → RAG 问答 → Web UI

## 目录结构

```
backend/
  app/            FastAPI 后端(agent、RAG、媒体解析、流式问答)
  frontend/       Web 前端(原生 JS)
  scripts/        数据管线(解密 / 导入 / 建向量 / 搜索 / 密钥扫描)
  server.py       服务入口(默认端口 8721)
  config.json     本机配置(含 API key,不入库;模板见 config.example.json)
data/             解密数据与 knowledge.db(私密,不入库)
models/           本地模型权重(不入库,见下方下载说明)
refs/             第三方参考项目(不入库)
需求文档.md / 项目文档.md
```

## 快速开始

环境要求:Windows 10/11、Python 3.11+、微信 4.0(已登录过、本地有聊天记录)。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置:复制模板并填入 DeepSeek API key
copy backend\config.example.json backend\config.json

# 3. 模型权重(不入库,需手动放到 models/ 下)
#    - models/bge-small-zh        中文向量模型,见 https://huggingface.co/maidalun1020/bge-small-zh-v1.5
#    - models/para-zh             sherpa-onnx 流式语音识别( Paraformer ),见 https://github.com/k2-fsa/sherpa-onnx/releases

# 4. 媒体解析依赖 ChatTrace,克隆到 refs/(可选,影响图片/.dat 解码)
git clone https://github.com/<ChatTrace 仓库地址> refs/ChatTrace

# 5. 数据管线(各脚本 --help 查看参数)
python backend/scripts/m0_decrypt.py        # 解密微信本地库 → data/
python backend/scripts/m0_import.py         # 解析消息 → data/knowledge.db
python backend/scripts/build_vectors.py     # 构建向量索引

# 6. 启动(或双击 启动助手.bat)
python backend/server.py 8721
# 浏览器访问 http://127.0.0.1:8721
```

详细技术方案见 [项目文档.md](./项目文档.md),需求背景见 [需求文档.md](./需求文档.md)。

## 安全与隐私

- `data/` 内含**解密后的聊天记录**与数据库密钥(`all_keys.json`),已在 `.gitignore` 中排除,**严禁提交或分享**
- `backend/config.json` 含真实 API key,不入库;仓库只保留 `config.example.json` 模板
- `models/`(约 521MB 权重)与 `refs/`(第三方仓库)同样不入库

## 路线图

- ✅ 一期:全量历史消息解密入库、语义/全文搜索、带引用 RAG 问答、语音/图片/链接解析
- ⏳ 二期:评估微信内小号查询入口(参考 wechaty),发送消息、防撤回、朋友圈均不做
