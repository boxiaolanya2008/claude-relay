# claude-relay

一个跑在本地的小代理,挂在 `claude-code` 和任意兼容 Anthropic SDK 的 API 之间。请求进来后改 model、改 system prompt、套请求/响应字段替换,再发到上游;响应回来后改 model 名、改 token 用量,按 SSE 事件粒度处理流式响应,然后还回去。

用途上最常见的一种是: 让 `claude-code` 以为自己跑的是 Sonnet,实际用更便宜的 Opus 或者国产中转模型。

## 适用场景

- 把 `claude-code` 接到第三方中转(OpenRouter、MiniMax、OneAPI、自建网关),不用动 `claude-code` 本身。
- 同一份 `claude-code` 配置里同时配多个 model 别名,代理层做映射。
- 在出口处压低 `usage` 数字,做账单或额度上的"看上去"。
- 演示或者录屏时,让流式响应带点延迟,有打字机效果。

如果只是想直连 Anthropic 官方,这个工具用不上。

## 依赖

- Python 3.10+
- `fastapi`, `uvicorn[standard]`, `httpx`, `python-dotenv`

```bash
python -m pip install -r requirements.txt
```

## 启动

```bash
python main.py
```

服务监听地址由 `.env` 里的 `PROXY_HOST` / `PROXY_PORT` 控制,默认 `127.0.0.1:8080`。所有配置项都在 `.env` 里改,改完重启进程生效。

让 `claude-code` 走代理:

```bash
# 临时: 启动时指定 base url
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 claude

# 持久: 写入 claude 配置
claude config set apiBaseUrl http://127.0.0.1:8080
```

`claude-code` 自己带过来的 API Key 会被代理丢弃,真正发到上游的是 `.env` 里的 `TARGET_API_KEY`。这一条是设计如此,不是 bug。

## 配置项

`.env` 一共 13 个变量,分四组。

### 服务监听

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_HOST` | `127.0.0.1` | 监听地址。保持 `127.0.0.1` 就够,改成 `0.0.0.0` 等于对外暴露,记得自己加鉴权 |
| `PROXY_PORT` | `8080` | 监听端口 |

### 上游 API

| 变量 | 默认 | 说明 |
|---|---|---|
| `TARGET_API_BASE` | `https://api.anthropic.com` | 目标 API 根地址,末尾的 `/` 会被自动去掉 |
| `TARGET_API_KEY` | (必填) | 真正发到上游的 Key。未设置时启动直接报错 |
| `TARGET_AUTH_HEADER` | `x-api-key` | 见下面"认证头"小节 |

**认证头**。`TARGET_AUTH_HEADER` 决定怎么把 Key 送上去,三种写法:

- `x-api-key` —— 直接发 `x-api-key: <key>`,Anthropic 官方格式。
- `authorization` —— 自动拼成 `Authorization: Bearer <key>`。
- `bearer` —— 等价于 `authorization`,只是名字不同。

`Authorization: Bearer` 拼好后大小写不敏感,大多数中转都认。少数只认小写 `authorization` 的中转把变量写成 `authorization` 就行。

### 模型与字段

`MODEL_MAP` 是代理的核心功能,逗号分隔多对 `请求模型:真实模型`:

```env
MODEL_MAP=claude-sonnet-4-6-20260603:claude-opus-4-8-20260528,claude-haiku-4-5:claude-opus-4-5
```

未在表里的 model 名原样转发。`/v1/models` 端点也只会返回表里出现过的 key。

`FIELD_REPLACEMENTS` 用于在响应里强行改写某些字符串字段:

```env
FIELD_REPLACEMENTS=model:claude-sonnet-4-6-20260603,service_tier:priority
```

- `model` 这一项是**必填的**,否则 `claude-code` 会看到上游真实的 model 名,SSE 流里也只在 `message_start` 这一帧改写响应体的 model。漏配的话账单对不上、或者直接被 `claude-code` 拒绝。
- 已存在的字段才会被替换,不会无中生有塞新 key。

### Token 用量

两套机制可以二选一或叠加:

```env
# 缩放: 上游 1000 token, SCALE=0.1, 返回 100
TOKEN_INPUT_SCALE=0.1
TOKEN_OUTPUT_SCALE=0.1

# 写死: 无论上游用了多少,都报成这个数(空则不用)
FAKE_INPUT_TOKENS=
FAKE_OUTPUT_TOKENS=
```

写死的优先级高于缩放。两者都空就走原始值。`usage` 字段在 `message_start` 和 `message_delta` 两个事件里都会被改写,所以流式过程里看到的累计 token 也是改过的数。

### 请求体 / 响应体替换

这两组比 `FIELD_REPLACEMENTS` 更灵活,值会用 `json.loads` 解析成原始类型,支持数字、布尔、对象、数组:

```env
# 发给上游之前改请求体
REQUEST_REPLACEMENTS=temperature:1.0,stream:false,max_tokens:4096

# 返回客户端之前改响应体
RESPONSE_REPLACEMENTS=stop_reason:end_turn
```

与 `FIELD_REPLACEMENTS` 的行为差异: `FIELD_REPLACEMENTS` 只替换已存在的字段;`RESPONSE_REPLACEMENTS` 会**强行写入**不存在的 key。`REQUEST_REPLACEMENTS` 同样会强行写入,这在覆盖 `temperature` 这种"必须存在但 claude-code 没传"的字段时很有用。

### System prompt 注入

```env
SYSTEM_PREFIX=
SYSTEM_SUFFIX=
```

给 `body.system` 加前后缀。Anthropic 的 `system` 字段有三种形态,代理都能处理:

- 缺省: 直接填 `prefix + suffix`。
- 字符串: `prefix + 原串 + suffix`。
- 列表 `[{type: "text", text: "..."}, ...]`: 只在第一个文本块前后拼接。
- 其他怪格式: 兜底 `json.dumps` 成字符串再拼。

### 流式延迟

```env
STREAM_DELAY_MS=50
```

上游的 SSE 事件快速接收,落到客户端时每发一个事件 `sleep` 这么久。`50ms` 大约是每秒 20 帧,看起来像打字机。`0` 表示不延迟。

延迟粒度是**事件级**不是字符级,所以一个大 `content_block` delta 也是按事件切,不会按 token 切。演示效果够用,做精确打字机模拟的话需要别的工具。

## 路由

| 路径 | 方法 | 行为 |
|---|---|---|
| `/v1/messages` | POST | 核心代理,流式和非流式都走这 |
| `/v1/models` | GET | 返回 `MODEL_MAP` 的 key 列表,`created_at` 写死 `2025-01-01` |
| 其他路径 | | 直接 404 |

`/v1/models` 是为了让 `claude-code` 启动时能做能力探测。如果 `MODEL_MAP` 是空,这个接口返回空列表,`claude-code` 启动会报 model 不存在。

## 中转平台示例

`TARGET_API_BASE` 和 `TARGET_AUTH_HEADER` 改两个变量就够,其它都不用动。

**Anthropic 官方**(默认配置即可):

```env
TARGET_API_BASE=https://api.anthropic.com
TARGET_API_KEY=sk-ant-api03-xxx
TARGET_AUTH_HEADER=x-api-key
```

**OpenRouter**:

```env
TARGET_API_BASE=https://openrouter.ai/api
TARGET_API_KEY=sk-or-v1-xxx
TARGET_AUTH_HEADER=authorization
```

**MiniMax / 国产中转**:

```env
TARGET_API_BASE=https://api.minimaxi.com/anthropic
TARGET_API_KEY=sk-xxx
TARGET_AUTH_HEADER=x-api-key
```

**OneAPI / NewAPI**:

```env
TARGET_API_BASE=https://your-api.example.com
TARGET_API_KEY=sk-xxx
TARGET_AUTH_HEADER=authorization
```

**自建网关**(已知上游要求 `anthropic-version` 头): 透传会带上 `claude-code` 原始的 `anthropic-version`,大部分情况不用动。少数中转对版本号敏感,需要的话在 `main.py:forward_headers` 里显式加一个 hardcode 透传。

## 实现细节

**请求体修改顺序** (`main.py:modify_request`):
1. 改 `model`(走 `MODEL_MAP`)
2. 注入 system prompt
3. 套 `REQUEST_REPLACEMENTS`

**响应体修改顺序** (`main.py:modify_response`):
1. 改 `model`(走 `FIELD_REPLACEMENTS`)
2. 改 `usage`
3. 套其它 `FIELD_REPLACEMENTS`
4. 套 `RESPONSE_REPLACEMENTS`

**SSE 流** (`main.py:sse_modify_stream`): 生产者-消费者模式,内部 `reader` 协程按行解析,只在 `message_start` 和 `message_delta` 两种事件上做 JSON 改写,其它事件原样透传。`STREAM_DELAY_MS` 控制下游吐事件的节奏。`response.aclose()` 在 `finally` 里,断开时不会泄漏上游连接。

**超时**: `connect=10s`, `read=300s`, `write=10s`, `pool=5s`。`read=300` 是按长对话的流式响应给的上限,普通调用不会触及。

**鉴权**: 本地代理,默认监听 `127.0.0.1`,没有任何 token 校验。`TARGET_API_KEY` 是直发上游的,不被本进程持久化,但 `.env` 本身要保管好——任何能读到这个文件的人就等于拿到上游 key。

## FAQ

**Q: 改了 `MODEL_MAP` 但 `claude-code` 启动还是报 model 不存在。**
A: 检查 `.env` 里 `MODEL_MAP=claude-sonnet-4-6-20260603:xxx` 的 key 必须和 `claude-code` 实际请求的 model 名完全一致(含日期后缀)。改完需要重启代理进程。

**Q: 流式响应里看到的是真实 model 名。**
A: `FIELD_REPLACEMENTS=model:你想要的名字` 必须配上,流式响应的 `message_start` 事件才会改 model。

**Q: token 用量看着对不上。**
A: 缩放(`TOKEN_*_SCALE`)和写死(`FAKE_*_TOKENS`)是叠加的,后者覆盖前者。两组都空才是原值。

**Q: 第三方中转报 400 说版本不对。**
A: 透传列表里只有 `anthropic-beta` 和 `anthropic-dangerous-direct-browser-access`,`anthropic-version` 走 httpx 默认行为(`claude-code` 一般发 `2023-06-01`)。如果中转要更新版本,在 `forward_headers` 里手动加一行。

**Q: 能不能监听 `0.0.0.0` 给团队用。**
A: 技术上可以,但本进程没有任何鉴权,等于把 `TARGET_API_KEY` 暴露给局域网。要做的话前面挂 nginx 加 basic auth 或者改代码加一层校验。

**Q: `.env` 里的 key 提交进 git 怎么办。**
A: 立即在对应平台后台 rotate,然后加 `.env` 进 `.gitignore`。本仓库 `.gitignore` 还没建,下一个版本会带上。

## 文件清单

| 文件 | 作用 |
|---|---|
| `main.py` | FastAPI 入口,请求/响应/SSE 拦截全在这 |
| `config.py` | `.env` 加载与解析 |
| `.env` | 配置(已包含示例值,生产环境务必覆盖) |
| `requirements.txt` | 4 个运行时依赖 |

## 限制

- 不做日志,出问题没有第一现场。下一个版本会加最小日志。
- 不做重试,上游 5xx 直接返给 `claude-code`。
- 不做 usage 累计,流式过程里每个事件看到的是独立的 token 数(已经被改写过)。
- `/v1/messages` 之外的 Anthropic 端点(`/v1/files` 等)不支持。
