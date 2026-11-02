<div align="center">

# 微信知识助手

**把本机微信聊天记录变成可搜索、可引用、可问答的私人知识库。**

全程本地运行 · 只读解密 · 不向微信发送任何消息

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white)
![WeChat](https://img.shields.io/badge/WeChat-4.x-07C160?logo=wechat&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-FTS5%20%2B%20vec-003B57?logo=sqlite&logoColor=white)
![Local First](https://img.shields.io/badge/data-stays%20local-2ea44f)

</div>

---

## 为什么需要它

微信里沉淀着真实的个人知识：私聊结论、群里的文件、语音里说过的地址、截图里的报错、收藏的文章。但微信自带的搜索只能匹配准确关键词，跨不过语音、图片、文件、链接这些媒介，也无法回答“我和某人之前聊过什么”。

这个项目做的事很简单：

> 解密本机微信数据库 → 把文字、语音、图片、文件、链接和朋友圈全部解析入库 → 提供语义搜索与带引用的 AI 问答。

每个回答都会带 `[1]`、`[2]` 这样的引用编号，点击直接跳到原始消息、文件或文章正文。聊天数据只在本机读写；外发请求仅限你配置的 LLM 接口，以及解析链接/朋友圈媒体时访问对应站点。

作者实测规模：**54 万条消息、827 个会话、1.8 万联系人**，检索毫秒级响应。

## 功能

| 能力 | 说明 |
|---|---|
| 语义 + 全文双路搜索 | 向量检索负责“意思对了”，FTS5 负责精确关键词与短词 |
| 带引用的 RAG 问答 | Agent 自动选择检索、统计、文件读取等工具；正文引用可点击溯源 |
| 语音消息转文字 | 使用 sherpa-onnx 本地离线识别，转写结果直接参与搜索和问答 |
| 图片 OCR | 截图、照片、名片、表格中的文字自动提取并建索引 |
| 链接内容解析 | 公众号、小红书、B 站等链接自动抓正文；图文做 OCR，视频取字幕或转写 |
| 朋友圈留存 | 正文、图片 OCR、评论、点赞同步入库；作者删除或“三天可见”不影响本地留存 |
| 实时同步 | 新消息通常 1 秒级进入知识库，可立即搜索和提问 |
| 密钥自愈 | 先用 HMAC 实测已有密钥；失效时扫描运行中的微信进程重新取钥，无需退出 |
| 本地优先 | 解密、索引、OCR、ASR 全部本地完成；不注入微信、不调用发送接口 |
| 一键迁移 | 内置备份/恢复脚本，聊天记录、索引、媒体缓存可原样搬到新电脑 |

## 架构

```mermaid
flowchart LR
  A[本机微信 4.x<br/>加密数据库] -->|ChatTrace 只读解密| B[消息 / 文件 / 媒体]
  B --> C[ETL：清洗 · OCR · ASR · 链接解析]
  C --> D[(knowledge.db<br/>SQLite + FTS5 + sqlite-vec)]
  D --> E[FastAPI 服务]
  E --> F[Web UI<br/>微信镜像界面]
  E --> G[LLM<br/>DeepSeek / OpenAI 兼容接口]
```

核心模块：

| 模块 | 职责 |
|---|---|
| `backend/app/main.py` | FastAPI API、会话/文件/链接/朋友圈、实时同步调度 |
| `backend/app/fastsync.py` | WAL 合并与页级增量解密；SQLCipher 直连导入新消息 |
| `backend/app/agent.py` | 把检索、统计、文件读取等能力包装成 LLM 工具，记录完整调用轨迹 |
| `backend/app/stream_qa.py` | SSE 流式问答：先快速检索，必要时升级为多轮 Agent |
| `backend/app/vector.py` | 本地 bge-small-zh 嵌入与 sqlite-vec 索引 |
| `backend/app/sns.py` | 朋友圈解析、永久留存、检索与媒体定位 |
| `backend/app/link_media.py` | 小红书 / B 站等链接的正文、OCR、字幕与语音转写 |
| `backend/frontend/` | 无框架的微信风格 Web UI |

## 快速开始

**环境要求**：Windows 10/11 · Python 3.11+ · 微信 4.x（本机登录过，且本地有聊天记录）

### 方式一：双击运行

1. 在 GitHub 页面选择 **Code → Download ZIP**，解压到任意目录。
2. 双击 **`一键配置.bat`**：安装依赖、生成配置、下载模型、执行自检。首次运行会要求填写一次 LLM API key。
3. 双击 **`启动助手.bat`**：浏览器打开 [http://127.0.0.1:8721](http://127.0.0.1:8721)。

### 方式二：命令行

```bash
git clone https://github.com/rhouselyn/wechat-knowledge-assistant.git
cd wechat-knowledge-assistant

python backend/scripts/setup.py        # 依赖 → 配置 → 模型 → 自检
python backend/server.py 8721          # 启动服务
```

配置脚本可以重复运行，已完成的步骤会自动跳过。非交互场景可使用：

```bash
python backend/scripts/setup.py --yes
python backend/scripts/setup.py --index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

> [!TIP]
> 首次启动后，服务会在后台依次解析：聊天文字、语音转写、文件正文、图片 OCR、公众号正文和最近一年的朋友圈。进度可在设置页查看；更早的历史可以随时在设置页续跑。

> [!NOTE]
> 朋友圈历史图片解密依赖可选的 Node.js 组件；缺少时核心功能不受影响，只会显示本地已缓存的图片。

<details>
<summary><b>本机没有历史数据？</b></summary>

一键配置检测到没有知识库时，会提供两个选择：

- 从旧电脑的备份包恢复；
- 从本机微信自动建库：增量解密 → 解析 → 导入，全程无需手敲命令。

数据库密钥通过 ChatTrace 在运行时捕获，属于账号级主密钥，跨微信重启稳定。服务会定期做 HMAC 实测；万一失效，会优先尝试扫描运行中的微信进程重新取钥。

</details>

> [!NOTE]
> macOS 支持为实验性：解密、导入、检索、朋友圈等核心逻辑可运行，但需要一次性手动导入 64 位 hex 密钥，且暂不支持内存扫钥。详见 [项目文档](项目文档.md)。

### 配置

`setup.py` 会在 `backend/config.json` 生成配置（该文件已被 git 忽略）。常用项：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `llm.base_url` | `https://api.deepseek.com` | OpenAI 兼容的接口地址 |
| `llm.api_key` | 空 | 你的 LLM API Key |
| `llm.model` | `deepseek-chat` | 日常问答模型 |
| `llm.thinking_model` | `deepseek-reasoner` | 开启“深度思考”时使用的推理模型 |
| `account.account_dir` | 自动发现 | 微信账号数据目录；多账号时建议手动指定 |
| `qa.recent_hours` | `24` | 会话内提问默认先检索最近多少小时 |
| `sync.realtime` | `true` | 是否开启实时同步 |

## 使用

- **聊天**：左侧会话列表与微信一致；中间渲染完整消息，支持图片、语音、视频、文件、合并转发和链接卡片。
- **搜索**：输入会话名过滤；回车执行全局消息搜索，结果可点击跳回上下文。
- **AI 问答**：右侧抽屉与“AI 助手”都是独立会话，按群聊、文件、链接分别隔离，不会串台。
- **提问方式**：`快速问答` 使用较少工具轮次（会话内 4 步、全局 6 步）；`探索模式` 使用更多轮次（8 / 12 步），适合统计、枚举、跨时段排查。
- **深度思考**：切换到推理模型，复杂问题更准，但响应更慢。
- **引用溯源**：回答中的 `[n]` 可点击跳到原消息；文件、链接、朋友圈各有独立引用样式。

## 维护命令

| 场景 | 命令 |
|---|---|
| 检查环境是否就绪 | `python backend/scripts/doctor.py` |
| 备份全部数据 | `python backend/scripts/backup_data.py D:\backup\wechat_data.zip` |
| 恢复备份 | `python backend/scripts/backup_data.py --restore D:\backup\wechat_data.zip` |
| 补漏重扫 | `python backend/scripts/rescan.py` |
| 重建向量索引 | `python backend/scripts/build_vectors.py` |
| 批量 OCR | `python backend/scripts/run_ocr.py` |
| 批量语音转写 | `python backend/scripts/run_asr.py` |
| 批量解析链接 | `python backend/scripts/index_links.py` |
| 只重建全文索引 | `python backend/scripts/rebuild_fts.py` |

> [!WARNING]
> `data/` 包含解密后的聊天记录、密钥和媒体缓存，`backend/config.json` 包含真实 API Key。两者都已在 `.gitignore` 中排除，请勿提交、上传或分享。

## 隐私与安全

- **数据不出本机**：解密、索引、OCR、ASR 全部在本地完成；外发请求仅限生成回答时的 LLM 调用，以及解析链接/朋友圈媒体时访问对应站点。
- **只读原则**：不调用微信任何发送接口，不修改微信数据库。
- **仓库不含隐私**：仓库只包含代码、示例配置和文档。
- **密钥本地保存**：Windows 下复用 ChatTrace 的 DPAPI 密钥库；macOS 下使用 Keychain 或 0600 权限文件。

## 故障排查

| 现象 | 处理方式 |
|---|---|
| 依赖安装失败 | 重新运行 `python backend/scripts/setup.py`，或加 `--index-url` 换源后重试 |
| 模型接口 401 / 403 | 检查 `backend/config.json` 中的 API Key 与模型权限 |
| 模型接口 429 | 触发限流，稍后重试或降低提问频率 |
| 模型接口超时 | 重试；复杂问题可先关闭“探索模式”或“深度思考” |
| 找不到微信数据目录 | 在 `backend/config.json` 的 `account.account_dir` 手动指定 |
| 密钥失效 / 无法解密 | 设置页查看密钥状态，必要时触发内存扫钥或重新导入 |
| 文件提示“不在本地磁盘” | 微信可能已清理缓存；可在微信中重新下载该文件 |
| 朋友圈图片缺失 | 部分历史图片依赖可选的 Node.js 解密组件，缺少时仅显示本地已缓存图片 |

## 技术栈

FastAPI · SQLite（FTS5 + sqlite-vec）· bge-small-zh 本地嵌入 · sherpa-onnx 离线语音识别 · RapidOCR 离线 OCR · [ChatTrace](https://github.com/qiaodogbear/ChatTrace) 解密（已内置）· 可选 Node.js + [WeFlow](https://github.com/hicccc77/WeFlow) wasm 朋友圈媒体解密

**明确不做**：发送消息、防撤回、多账号托管、商业化。

## 致谢

- [ChatTrace](https://github.com/qiaodogbear/ChatTrace)：微信 4.x 数据库解密与密钥捕获（MIT，已内置）
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)：离线语音识别
- [BGE / BAAI](https://huggingface.co/BAAI/bge-small-zh-v1.5)：中文向量模型
- [WeFlow](https://github.com/hicccc77/WeFlow)、[chatlog](https://github.com/sjzar/chatlog)、[PyWxDump](https://github.com/xaoyaoo/PyWxDump)、[WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)：路线调研与实现思路参考

## 免责声明

本项目仅供个人本地数据备份与知识管理学习研究使用。请妥善保管解密后的数据与密钥，勿用于侵犯他人隐私、数据倒卖等非法用途；因使用不当造成的任何后果由使用者自行承担。
