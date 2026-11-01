# ls agent

一个跑在本机的 Python AI Agent：**自带 Web 前端、可接入任意大模型、技能（Skill）可插拔**。

不依赖前端构建工具，不依赖任何 Agent 框架 —— 核心只有一个 Agent 循环 + 工具注册表 + SSE 流式接口。

```
浏览器（原生 HTML/CSS/JS）
      │  POST /api/chat  (SSE 流式)
      ▼
FastAPI ──► Agent 循环 ──► 模型（OpenAI 兼容 / Claude / Ollama / Mock）
                 │  ▲
                 ▼  │
            工具执行器 ──► 文件 / 代码 / HTTP / 技能
```

---

## 快速开始

```bash
python -m pip install -r requirements.txt
python run.py --open          # 默认 http://127.0.0.1:8000
```

首次启动默认使用 **Mock 演示模型**（不需要 API Key）：在首页点「查时间（演示工具调用）」就能看到完整的
「模型 → 工具 → 模型 → 回答」链路，包括工具卡片与流式输出。

接真实模型：右上角 **设置 → 模型**，填 Base URL / 模型名 / API Key，点「保存」再点「测试连接」，
然后把该 provider 设为「使用中」。

想给它换个名字：**设置 → 通用 → 助手名称**，保存后浏览器标题、侧栏与 Logo 一起变（当前叫 `ls agent`）。

---

## 接入模型

「模型」面板里每张卡片就是一个 provider，支持三种类型：

| type | 说明 | Base URL 示例 | 模型示例 |
| --- | --- | --- | --- |
| `openai` | 任意 OpenAI 兼容接口 | `https://api.openai.com/v1` | `gpt-4o-mini` |
| `openai` | DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| `openai` | Moonshot | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| `openai` | 通义千问兼容模式 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| `openai` | Ollama 本地 | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |
| `openai` | vLLM / LM Studio | `http://127.0.0.1:8000/v1` | 你的模型名 |
| `anthropic` | Claude Messages API | `https://api.anthropic.com` | `claude-sonnet-4-20250514` |
| `mock` | 演示模型（验证链路用） | — | — |

- API Key 也可以不填，改在 `api_key_env` 里写环境变量名（如 `OPENAI_API_KEY`），启动服务前 `set` 好即可。
- Key 只保存在本机 `data/config.json`，所有接口返回时都会脱敏（只回 `has_key: true/false`）。
- 切换模型不会丢上下文：会话里保存的是标准消息格式，OpenAI / Claude 会自动做格式转换。

---

## 写一个技能（Skill）

技能就是 `skills/<slug>/SKILL.md`，可带 `scripts/*.py`：

```
skills/
└── research-brief/
    ├── SKILL.md
    └── scripts/            # 可选，用 run_skill_script 执行
        └── fetch.py
```

`SKILL.md` 格式：

```markdown
---
name: 深度调研简报
description: 把问题拆成子问题、抓取公开信息并输出结构化简报（模型靠这句话判断何时触发）
tags: research, writing
always: false
---

# 深度调研简报

## 适用场景
...

## 执行步骤
1. 先拆题
2. 用 http_get 抓来源
3. ...
```

生效机制：

1. **启用**（技能面板开关）后，`description` 会写进系统提示词，模型知道「有这个技能、什么时候用」；
2. 任务命中时，模型调用 `load_skill` 读取完整正文，再按步骤执行；
3. `always: true` 的技能会把正文**始终**注入系统提示词（适合全局规范）；
4. 技能带脚本时，模型可以 `run_skill_script` 在技能目录里跑 Python。

内置四个示例技能：`research-brief`（调研简报）、`web-search`（联网检索，含批量抓取脚本）、`code-review`（代码审查）、`data-analyst`（CSV 描述性统计，含脚本）。

也可以在 **技能 → 新建技能** 里直接创建，或手动放目录后刷新页面。

---

## 联网检索（web-search）

不用额外 Key、不用注册搜索服务：`web_search` 直接读 Bing 的搜索结果（RSS 优先，HTML 兜底），
`http_get` 负责打开原文，`web-search` 技能负责把这两步编排成「先搜后答、必须给链接」的流程。

- 在 **技能** 面板启用 `web-search`（`description` 会进系统提示词），时效类问题模型就会自己搜。
- 模型的行为链是：`web_search`（拿到标题 / 真实链接 / 摘要）→ 挑 2–3 条 `http_get` 读原文 → 交叉验证 → 结论 + 来源清单。
- 来源多（≥4 条）时，技能会改用 `run_skill_script` 跑 `skills/web-search/scripts/fetch_pages.py`，
  把正文批量存到 `workspace/web-cache/<时间戳>/`，再挑文件精读，省上下文。

试试这些：

```
帮我查一下 Python 3.14 的正式发布日期，给出来源链接
对比一下 DeepSeek 和 Qwen 最新旗舰模型的上下文长度，附官网出处
今天科技圈有什么大事？给 3 条，附链接
```

边界与隐私：

- 只有搜索关键词和你要打开的 URL 会发给 `bing.com` / 目标站点，不带账号、不带 Key；UA 里保留 `lsagent/0.1` 标识。
- 不登录、不绕验证码；被反爬拦了就换关键词或换来源，模型会在答案里说明「未读到原文」。
- JS 渲染的页面抓不到正文，只能拿到搜索摘要，这一点会体现在答案的可信度标注里。

---

## 内置工具

| 工具 | 作用 |
| --- | --- |
| `get_time` | 当前本地/UTC 时间、时间戳、星期 |
| `calculate` | 安全的数学表达式求值（AST 白名单，不走 eval） |
| `list_dir` / `read_file` / `write_file` | 工作区内的文件读写（禁止路径穿越） |
| `http_get` | 抓公开网页/API：优先取正文区（`<article>`/`<main>`），HTML 转纯文本，自动适配 utf-8 / gb18030 |
| `web_search` | 联网搜索关键词（Bing，免 Key），返回标题、真实链接、摘要；`site` 可限定站点 |
| `run_python` | 在工作区执行 Python（20s 超时，捕获 stdout/stderr） |
| `list_skills` / `load_skill` / `run_skill_script` | 技能发现、读取正文、执行脚本 |

工具可在 **设置 → 工具** 中随时禁用；新增工具只要写一个函数并加 `@tool(...)` 装饰器，模型立刻可用。

```python
@tool("word_count", "统计一段文本的字数", {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
})
def word_count(args, ctx):
    return f"{len(args['text'])} 个字符"
```

---

## 目录结构

```
app/
  main.py          FastAPI：REST + SSE + 静态资源
  agent.py         Agent 循环（模型 → 工具 → 模型，最多 N 步）
  llm.py           模型接入层：OpenAI 兼容 / Anthropic / Mock，流式解析
  tools/
    __init__.py    工具注册表、执行器、工具 Schema
    builtin.py     内置工具
    skill_tools.py 技能桥接工具
  skills.py        SKILL.md 解析、启停、系统提示词拼装
  storage.py       会话 JSON 持久化
  logging_setup.py 日志：控制台 + 轮转文件 + 面板用内存缓冲 + 密钥打码
  config.py        配置与 provider 管理（Key 脱敏）
  static/          前端（index.html / app.js / style.css）
skills/            技能目录（含 web-search 联网检索：SKILL.md + scripts/fetch_pages.py）
workspace/         文件与代码工具的活动沙箱目录
data/              config.json + sessions/ + logs/（运行时生成）
tests/smoke_test.py 端到端自检
run.py             启动入口
```

---

## 常用接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat` | SSE 流式对话，事件：`session` `step` `delta` `reasoning` `assistant` `tool_call` `tool_result` `notice` `error` `done` |
| GET/PUT | `/api/config` | 读取（脱敏）/ 更新配置 |
| GET/POST/PUT/DELETE | `/api/providers...` | 模型提供商增删改查、`/test` 连通性测试 |
| GET/POST | `/api/sessions` | 会话列表 / 新建 |
| GET/PATCH/DELETE | `/api/sessions/{id}` | 会话详情 / 改名换模型 / 删除 |
| POST | `/api/sessions/{id}/clear` | 清空消息 |
| GET/POST/PUT/DELETE | `/api/skills...` | 技能增删改查、`/toggle` 启停 |
| GET | `/api/tools` | 工具清单 |
| GET | `/api/logs?limit=&level=&search=` | 最近日志（内存里的 500 条，可按级别/关键字过滤） |
| DELETE | `/api/logs` | 清空日志（内存 + 截断文件） |
| GET | `/api/logs/download` | 下载日志文件 |

完整文档见 `http://127.0.0.1:8000/docs`。

---

## 日志

不用装 loguru，标准库够了。三个出口，一份配置管到底：

| 出口 | 位置 | 用途 |
| --- | --- | --- |
| 控制台 | 启动服务的终端 | 边用边看，出错时最快 |
| 文件 | `data/logs/ls-agent.log` | 事后回查；默认 2 MB 轮转、保留 5 份（`ls-agent.log.1` …） |
| 内存 | 最近 500 条 | 前端 **设置 → 日志** 面板直接看，SSE 出问题不用翻终端 |

看得见的信息（每行都带一个 8 位请求 id，一轮对话的所有日志共用同一个 id）：

```
2026-10-28 18:02:38 INFO  [e06662d0] lsagent.chat:  对话开始 · session=705f7e1e51d1
2026-10-28 18:02:38 INFO  [e06662d0] lsagent.agent: 开始一轮 · provider=deepseek(deepseek-chat) · 工具 11 个 · 上限 8 步 · 输入 12 字
2026-10-28 18:02:55 INFO  [e06662d0] lsagent.agent: 第 1 步模型返回 · 文本 83 字 · 工具调用 0 个 · 首字 14459 ms · 合计 17020 ms
2026-10-28 18:02:55 INFO  [e06662d0] lsagent.agent: 本轮结束 · 共 1 步 · 成功
2026-10-28 18:02:38 INFO  [98bf007b] lsagent.http:  POST /api/sessions → 200 · 4 ms
```

- **请求**：方法、路径、状态码、耗时；4xx/5xx 自动升到 WARNING，静态资源不记。
- **模型**：provider / 模型名、上下文条数、首字延迟、总耗时；HTTP 错误连关键字段一起记。
- **工具**：工具名、耗时、结果字数；失败时记错误摘要（参数非法、异常栈）。
- **技能**：启用 / 停用 / 删除。

怎么调（**设置 → 通用**，改完立即生效，不用重启）：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `log_level` | `INFO` | `DEBUG` 能看到每一步的模型请求、工具参数细节；`WARNING` 只看问题 |
| `log_file` | `data/logs/ls-agent.log` | 相对项目根；留空（或 `off`）就只走控制台 |
| `log_max_mb` / `log_backups` | `2` / `5` | 单文件上限、历史份数 |
| `log_console` | `true` | 关掉后只剩文件 + 面板（适合后台常驻） |
| `log_prompts` | `false` | 是否把对话原文写进日志。默认只记字数，避免日志变成聊天记录 |

隐私上做了三件事：写入前统一过 `redact()`，`sk-...`、`Bearer ...`、`api_key: ...` 一律打成 `sk-***abcd`；默认不记对话原文；
`data/` 整个目录都在 `.gitignore` 里，日志不会被提交。

环境变量兜底（容器 / 自测里用）：`LSAGENT_LOG_FILE=/tmp/ls.log`、`LSAGENT_LOG_CONSOLE=0`。

小技巧：`http://127.0.0.1:8000/?tab=logs` 直接打开日志面板（`#logs` 也行）。

---

## 常见报错

报错会被翻译成「原因 + 当前配置 + 下一步」三部分，上游原始返回附在最后一行，照着改就行。

| 现象 | 真实原因 | 怎么修 |
| --- | --- | --- |
| `HTTP 401 Authentication Fails (governor)` | 网关不认这个 Key：无效 / 过期 / 被冻结，或鉴权头不对 | 重新粘贴 Key 后点「测试连接」；OpenAI 兼容网关把类型设成 `openai`（Bearer），Claude 官方用 `anthropic`（x-api-key） |
| `HTTP 403` | Key 没有该模型的权限，或账户欠费 | 换模型或充值 |
| `HTTP 404` | Base URL 多了或少了 `/v1` | OpenAI 兼容一般写到 `/v1` 结尾；Claude 官方是 `https://api.anthropic.com` |
| `HTTP 429`（`TPM limit exceeded`） | 限流 / 额度用尽 | 等几十秒再发，或换 Key |
| `未配置 ... API Key` | Key 与 `api_key_env` 都为空 | 在设置里填 Key，或设对应环境变量 |
| `No module named 'fastapi'` | 依赖没装 | `python -m pip install -r requirements.txt` |
| `搜索失败：Bing RSS：0 条结果` / `网页：0 条结果` | 关键词太生僻，或搜索页触发了反爬 | 换更短的关键词、换英文再搜；已知网址时直接用 `http_get` |
| 抓回来的网页只有导航没有正文 | 页面靠 JS 渲染 | 换同主题的文档站/静态页；或直接调用它背后的 JSON 接口 |
| 日志文件写不进去（`PermissionError`） | 目标目录只读或被别的进程占着 | 程序会自动降级为只输出控制台；把 `log_file` 换成可写路径，或直接留空 |
| 终端里的日志中文是乱码 | Windows 控制台是 GBK，日志文件是 UTF-8 | 文件内容没问题，看 **设置 → 日志** 面板或点「下载文件」；或先 `chcp 65001` |

排错顺序建议：先点 **设置 → 模型 → 测试连接**（只发一句 ping，能立刻区分「Key/网络问题」还是「对话逻辑问题」），
再看 **设置 → 日志** 面板（同一个面板里能按级别过滤、搜关键字），需要长历史就翻 `data/logs/ls-agent.log`。

## 自测

```bash
python tests/smoke_test.py
```

会用 Mock 模型跑完「会话 → SSE → 工具调用 → 工具结果 → 持久化」，并检查技能 CRUD、
路径穿越拦截、`run_python`、静态资源、联网搜索解析、日志（密钥打码 / 级别过滤 / 清空）等共 49 项检查。

自测期间的日志写到系统临时目录，不会动你的 `data/logs/ls-agent.log`。

自测**不依赖本机配置**：没有 mock provider 时会临时建一个（跑完自动删除），
所以删掉所有 provider、或换到真实模型之后依然能跑。

发布前再加一道：

```bash
python tests/privacy_check.py
```

扫描 Key、本机绝对路径、邮箱、私钥块，退出码 0 才算干净。

---

## 安全说明（重要）

- 这是**本机单用户**工具：没有登录鉴权，不要绑定 `0.0.0.0` 暴露到公网。
- `run_python` 能执行任意 Python 代码（有工作目录限制和超时，但不是真正的沙箱）。
  不要把服务交给不可信的人使用。
- 文件工具被限制在 `workspace/` 内，`..` 越界会被拒绝。
- API Key 以明文存放在 `data/config.json`，该目录已加入 `.gitignore`。

## 发布到 GitHub

仓库里**不含任何个人信息**：没有 Key、没有本机绝对路径、没有邮箱和用户名，路径全部是相对路径。
`data/`（Key + 会话）、`workspace/`（除示例 CSV）、`.agnes/` 都在 `.gitignore` 里，不会被提交。

发布前跑一次自检：

```bash
python tests/privacy_check.py         # 只查将要提交的文件
python tests/privacy_check.py --all   # 连 data/ 一起查（确认本地 Key 长什么样，别提交）
```

它会把 `sk-...` 密钥、`C:\Users\...` 本机路径、邮箱、私钥块都当可疑内容列出来，退出码 1 表示需要处理。

首次推送到 GitHub：

```bash
git init
git add .
git status            # 确认列表里没有 data/、workspace/ 下的个人文件
git commit -m "feat: ls agent - 可插拔技能的本地 Python Agent"
git branch -M main
git remote add origin git@github.com:<你的用户名>/ls-agent.git
git push -u origin main
```

没装 git 的话，用 GitHub 网页的 **Add file → Upload files** 直接拖这个目录上传即可
（`data/`、`__pycache__/` 记得排除；也可以让我导出成一个干净的 zip）。

两个约定：

- **新用户第一次运行**会自动生成 `data/config.json`（默认 Mock 模型，无需 Key）。想提前看配置结构，
  参考仓库里的 `config.example.json`：所有 `api_key` 都是空串，Key 走环境变量或界面填写。
- 万一曾经把真 Key 提交过：**先去服务商后台作废该 Key**，再 `git rebase`/`filter-repo` 清历史——
  只删文件不改历史是不够的，历史里还留着。

## 可以继续加的东西

- 工具调用前的「人工确认」开关（危险工具二次确认）
- 长期记忆 / 向量检索（把历史摘要注入提示词）
- 多模态（图片输入）、MCP 客户端、技能市场
- 多用户 + Token 鉴权，部署到内网


## Router + Worker：动手的活派成工单

一条消息进来，先由 `app/router.py` 判定，再决定谁来干：

```
你说话 -> Router
          |- 问句 / 闲聊 / 太短 -> 主会话直接回答（历史照常累积）
          |- 动手类（改、实现、修复、重构……）-> 建工单，交执行器
```

- **主会话**（`kind=main`）只留一行记录：你的原话 + `已派工 #id`。执行过程不复制进来。
- **执行会话**（`kind=worker`）每张工单一个，历史从零开始、互不干扰，结束后封存。
  这是省 token 的主要来源：工具结果不再长期留在主对话里反复重发。
- **工单** `data/tasks/<id>.json`：请求、范围、授权（Mandate：策略版本 / 范围 / 强度）、
  派工记录、回报（progress / waiting / ready / blocked / failed / dropped）、会话 id。
  它自己定义状态，不跟随任何会话，所以可换执行器、可恢复。
- **阶段**由 `tasks.stage()` 按顺序判定：`person/answer` → `working/queued` →
  `person/review` → `person/blocked|failed` → `idle` → `working` → `done`。
  人回一句话会解锁 `blocked` / `failed`（走 `add_input`，不新开工单）。
- **前端**右上角窄栏只显示 `person/*` 的工单，健康状态不占位置。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tickets` | 工单列表（带 stage）与 `pending` 计数 |
| GET | `/api/tickets/{id}` | 单张工单详情 |
| POST | `/api/tickets/{id}/stop` | 人工停止（回报 dropped） |

设计取自 DeepSeek Super-agent（Router/Worker 分层、Ticket 独立状态、先落盘再执行），
按其文档的说法：Session 是实现细节，任务是独立实体。

## 设计取舍

照搬了：工单独立状态、授权与上下文分离、人话解锁受阻工单、先落盘再执行（`tasks.enqueue`）。
没有照搬：Policy 版本化的完整生命周期、跨 Source 身份映射 —— 那解决的是团队授权问题，
单人使用只会变成没人填的字段。
