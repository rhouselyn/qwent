<div align="center">

# 微信知识助手

**把你的微信聊天记录,变成一个能语义搜索、带引用问答的私人知识库**

*全程本地运行 · 只读不发包 · 零封号风险*

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-10%20%2F%2011-0078D4?logo=windows11&logoColor=white)
![WeChat](https://img.shields.io/badge/%E5%BE%AE%E4%BF%A1-4.x-07C160?logo=wechat&logoColor=white)
![Privacy](https://img.shields.io/badge/%E6%95%B0%E6%8D%AE-%E5%85%A8%E7%A8%8B%E6%9C%AC%E5%9C%B0-8A2BE2)

</div>

---

## 💡 为什么需要它

几年下来,微信里沉淀了几十万条消息、文件、语音、图片和链接——那是你真实的个人知识资产。但你一定遇到过:

- 🗣️ "朋友语音里说过的一个地址",翻两百条语音还没找到
- 📄 "明明发过一份合同",只记得大概聊过,记不住原文用什么词
- 🖼️ 截图里的报错、名片、表格,微信搜索永远搜不到
- 🔗 群里分享过的文章和视频,内容既搜不到,也没法"问"
- ❓ 最关键的:**没有任何工具能回答"我和某某聊过什么"这种问题**

微信自带的搜索只能匹配准确关键词,跨不过语音、图片、链接这些媒介。本项目把它们全部补齐:

> 解密本地数据库 → 文字/语音/图片/文件/链接**全部入库建索引** → AI 问答,
> **每个答案都带 `[n]` 引用编号,点击直达原始消息、文件或链接原文**。

## ✨ 核心特性

| | 特性 | 说明 |
|---|---|---|
| 🔍 | **语义 + 全文双路搜索** | 向量检索 + 全文索引,意思对了就能搜到,不必逐字匹配 |
| 🤖 | **带引用溯源的 RAG 问答** | AI 自动检索消息/文件/链接组织答案,正文 `[n]` 可点击跳回原文 |
| 🎙️ | **语音消息全文可搜** | 离线语音转文字,气泡下方直接显示 |
| 🖼️ | **图片内容可搜** | OCR 提取截图/照片中的文字并建索引 |
| 🔗 | **链接内容可搜可问** | 公众号、小红书、B 站内容自动解析入库 |
| 🌄 | **朋友圈永久留存** | 自动同步朋友圈(正文/图片 OCR/评论/点赞)进知识库;作者删除或"三天可见"不影响本地留存,左侧栏浏览、可问 AI |
| ⚡ | **实时同步** | 新消息约 1 秒自动入库,可检索、可问答 |
| 🛡️ | **零封号风险** | 只读本地文件,不向微信发送任何消息 |
| 📦 | **开箱即用** | 一个脚本完成依赖、配置、模型下载与自检,全程显示进度 |
| 🔁 | **一比一迁移** | 内置备份/恢复工具,换电脑原样搬走 |

> 作者实测规模:54 万条消息、1.8 万联系人、827 个会话,检索毫秒级响应。

## 🚀 快速开始

**环境要求**:Windows 10/11(完整体验) · macOS(实验性,见下) · Python 3.11+ · 微信 4.x(本机登录过、本地有聊天记录)

### 方式一:纯双击(不碰命令行)

1. GitHub 页面右上角 **Code → Download ZIP**,解压到任意目录
2. 双击 **`一键配置.bat`** —— 自动装依赖、生成配置(填一次 DeepSeek API key)、下载模型、自检
3. 双击 **`启动助手.bat`** —— 浏览器打开 `http://127.0.0.1:8721`,开始使用

### 方式二:纯命令行

```bash
git clone https://github.com/rhouselyn/wechat-knowledge-assistant.git
cd wechat-knowledge-assistant
python backend/scripts/setup.py        # 一键:依赖 → 配置(交互填 key)→ 模型 → 自检
python backend/server.py 8721          # 启动,浏览器打开 http://127.0.0.1:8721
```

> 两种方式效果完全相同。配置脚本可重复运行,已完成的步骤自动跳过;
> 命令行版支持 `--yes`(非交互)和 `--index-url`(换 pip 源)。

**首次配置后自动后台解析**:启动服务即自动按顺序执行——聊天文字(建库即全量)、
**语音转写、文件正文(全量历史)**,图片 OCR、公众号正文、朋友圈(**最近一年**);
进度在设置页可见,更早的历史想补充解析,在设置页随时续跑。

<details>
<summary><b>全新环境首次建库</b>(本机没有历史数据时,点击展开)</summary>

一键配置检测到没有知识库时会自动询问,二选一:**恢复旧电脑的备份包** 或 **从本机微信自动建库**
(增量解密 → 解析导入,全程无需手动敲命令)。

数据库密钥通过 ChatTrace 运行时捕获,密钥账号级稳定(跨微信重启不变),
服务会定期 HMAC 实测有效性,正常情况**永远不需要重抓**;
万一实测失效,优先**扫描运行中的微信进程内存**重新取钥(约 34 秒,免退出),
扫描未命中才需要退出微信重新登录抓取一次(仅首次建库或极端情况会遇到)。
多账号可在 `backend/config.json` 的 `account.account_dir` 手动指定。

</details>

> 可选组件:朋友圈的**历史图片**来自微信 CDN,解密依赖 Node.js 运行时
> (一键配置会自动下载解密组件;没有它朋友圈仍可用,只是图片仅限本地缓存过的部分)。

### macOS(实验性)

解密、导入、语义搜索、朋友圈、实时同步等核心全部跨平台;差异只在密钥获取:
mac 上没有内存扫钥方案,需**一次性手动导入密钥**(密钥账号级稳定,导一次长期有效,
服务端自动做 HMAC 自愈检测):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 密钥:用任意 mac 取钥工具拿到 64 位 hex 后导入(自动 HMAC 校验)
python backend/app/keyvault.py --account-dir "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/wxid_xxx" --import <64hex>
python backend/server.py 8721
```

找不到账号目录时在 `backend/config.json` 的 `account.account_dir` 手动指定。

## 📦 换新电脑

```bash
# 旧电脑:打包聊天记录(服务运行中也可备份)
python backend/scripts/backup_data.py D:\backup\wechat_data.zip

# 新电脑:clone → 一键配置 → 恢复 → 启动
python backend/scripts/backup_data.py --restore D:\backup\wechat_data.zip
```

恢复后与原电脑完全一致:聊天记录、向量索引、媒体缓存原样搬走。

## 🔒 隐私与安全

- **数据不出本机**:解密、索引、语音转写、OCR 全部本地完成;唯一外发请求是调用你配置的 LLM 生成回答
- **仓库不含任何隐私**:聊天记录与 API key 均被排除在外,仓库只有代码
- **只读原则**:不调用微信任何发送接口、不修改微信数据

## 🏗️ 技术栈

FastAPI · SQLite(FTS5 全文 + sqlite-vec 向量)· bge-small-zh 本地嵌入 · sherpa-onnx 离线语音识别 · RapidOCR 离线 OCR · [ChatTrace](https://github.com/qiaodogbear/ChatTrace) 解密(已内置)· 朋友圈媒体 ISA64 流解密(Node.js + [WeFlow](https://github.com/hicccc77/WeFlow) wasm,可选组件)

## 🗺️ 路线图

- ✅ 全量历史解密入库、语义/全文搜索、带引用 RAG 问答、语音/图片/链接解析、实时同步(~1 秒)
- ✅ 朋友圈永久留存与检索(正文/图片 OCR/评论/点赞/分享文章,独立页面 + AI 问答工具)
- ✅ 密钥自愈(HMAC 实测代替 24h 过期;失效时内存扫钥免退出重取,4.1.15 实测 34 秒)
- ✅ macOS 实验性支持(一次性导入密钥,见上)
- ⏳ 微信内小号查询入口(评估中) · mac 原生取钥(需真机调试)
- ❌ 明确不做:发送消息、防撤回

## 🙏 致谢

[ChatTrace](https://github.com/qiaodogbear/ChatTrace)(MIT,已内置)· [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) · [BGE (BAAI)](https://huggingface.co/BAAI/bge-small-zh-v1.5) · 调研参考 [WeFlow](https://github.com/hicccc77/WeFlow) / [chatlog](https://github.com/sjzar/chatlog) / [PyWxDump](https://github.com/xaoyaoo/PyWxDump) / [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis)(朋友圈媒体缓存命名规则与内存锚点取钥思路)

## ⚠️ 免责声明

本项目仅供**个人本地数据备份与知识管理**学习研究使用。请妥善保管解密后的数据与密钥,
勿用于侵犯他人隐私、数据倒卖等非法用途;因使用不当造成的任何后果由使用者自行承担。
