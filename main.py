import asyncio
import json
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from contextlib import asynccontextmanager
from config import config

EVENTS_TO_MODIFY = {"message_start", "message_delta"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0)
    )
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan)


def forward_headers(req: Request) -> dict[str, str]:
    headers = {"content-type": req.headers.get("content-type", "application/json")}

    if config.target_auth_header.lower() == "bearer":
        headers["authorization"] = f"Bearer {config.target_api_key}"
    else:
        headers[config.target_auth_header] = config.target_api_key

    for h in ("anthropic-beta", "anthropic-dangerous-direct-browser-access"):
        if h in req.headers:
            headers[h] = req.headers[h]
    return headers


# ---------------------------------------------------------------------------
# 请求拦截: 在发给上游之前修改请求体
# ---------------------------------------------------------------------------

def modify_request(body: dict) -> dict:
    """Intercept and mutate the request body before forwarding upstream."""

    # 1. model 映射
    if "model" in body:
        body["model"] = config.map_model(body["model"])

    # 2. system prompt 注入
    if config.system_prefix or config.system_suffix:
        _inject_system(body)

    # 3. JSON 字段替换(REQUEST_REPLACEMENTS)
    for key, value in config.request_replacements.items():
        body[key] = value

    return body


def _inject_system(body: dict) -> None:
    """给 system prompt 加前缀/后缀. Anthropic 格式: body['system'] 是 str 或 list."""
    prefix = config.system_prefix
    suffix = config.system_suffix
    system = body.get("system")

    if system is None:
        body["system"] = prefix + suffix
        return

    if isinstance(system, str):
        body["system"] = prefix + system + suffix
        return

    # list 格式 [{"type": "text", "text": "..."}, ...]
    if isinstance(system, list) and system:
        first = system[0]
        if isinstance(first, dict) and first.get("type") == "text":
            first["text"] = prefix + first["text"] + suffix
        else:
            # 不认识的格式, 转成字符串兜底
            body["system"] = prefix + json.dumps(system) + suffix
    else:
        body["system"] = prefix + str(system) + suffix


# ---------------------------------------------------------------------------
# 响应拦截: 上游返回后修改再发给客户端
# ---------------------------------------------------------------------------

def _fake_usage(u: dict) -> None:
    """根据配置修改 usage 字典(就地修改)."""
    if config.fake_input_tokens is not None:
        u["input_tokens"] = int(config.fake_input_tokens)
    elif "input_tokens" in u:
        u["input_tokens"] = int(u["input_tokens"] * config.token_input_scale)

    if config.fake_output_tokens is not None:
        u["output_tokens"] = int(config.fake_output_tokens)
    elif "output_tokens" in u:
        u["output_tokens"] = int(u["output_tokens"] * config.token_output_scale)


def modify_response(data: dict) -> dict:
    # 1. model 替换(兼容旧 FIELD_REPLACEMENTS)
    if "model" in data:
        data["model"] = config.field_replacements.get("model", data["model"])

    # 1.5 请求 ID 写死: 无论上游发多少次, 都返回同一个 id
    if config.fake_request_id and "id" in data:
        data["id"] = config.fake_request_id

    # 2. token 用量伪装
    if "usage" in data:
        _fake_usage(data["usage"])

    # 3. 字符串字段替换(兼容旧 FIELD_REPLACEMENTS)
    for field, value in config.field_replacements.items():
        if field != "model" and field in data:
            data[field] = value

    # 4. JSON 级字段替换(RESPONSE_REPLACEMENTS, 支持任意类型)
    for key, value in config.response_replacements.items():
        data[key] = value

    return data


def modify_sse_event(event_type: str, data: dict) -> dict:
    if event_type == "message_start":
        msg = data.get("message")
        if msg:
            if "model" in msg:
                msg["model"] = config.field_replacements.get("model", msg["model"])
            if config.fake_request_id and "id" in msg:
                msg["id"] = config.fake_request_id
            # message_start 里的 usage.input_tokens 也要伪装
            usage = msg.get("usage")
            if usage:
                _fake_usage(usage)

    elif event_type == "message_delta":
        usage = data.get("usage")
        if usage:
            _fake_usage(usage)

    return data


async def sse_modify_stream(response):
    """生产者-消费者: 上游快速接收, 下游按 STREAM_DELAY_MS 限速渲染.
    response 生命周期由本生成器管理, 在 finally 中关闭."""
    queue = asyncio.Queue(maxsize=200)
    delay = config.stream_delay_ms / 1000.0

    async def reader():
        buf = b""
        current_event = None
        event_buf = []

        async def flush():
            if event_buf:
                await queue.put(("chunk", "".join(event_buf)))
                event_buf.clear()

        try:
            async for chunk in response.aiter_bytes():
                buf += chunk
                *lines, buf = buf.split(b"\n")

                for line_bytes in lines:
                    line = line_bytes.decode("utf-8")

                    if line.startswith("event: "):
                        await flush()
                        current_event = line[7:].strip()
                        event_buf.append(line + "\n")

                    elif line.startswith("data: "):
                        payload = line[6:]
                        if current_event in EVENTS_TO_MODIFY:
                            try:
                                obj = json.loads(payload)
                                obj = modify_sse_event(current_event, obj)
                                payload = json.dumps(obj, separators=(",", ":"))
                            except json.JSONDecodeError:
                                pass
                        event_buf.append(f"data: {payload}\n")

                    elif line == "":
                        event_buf.append("\n")
                        await flush()
                        current_event = None

                    else:
                        event_buf.append(line + "\n")

            if buf:
                event_buf.append(buf.decode("utf-8"))
            await flush()

        except (httpx.StreamClosed, asyncio.CancelledError):
            pass
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(reader())

    try:
        while True:
            kind, payload = await queue.get()
            if kind == "done":
                break
            yield payload.encode("utf-8")
            if delay > 0:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        await response.aclose()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def proxy_messages(request: Request):
    body = await request.json()
    headers = forward_headers(request)

    # 请求拦截: 改完再发上游
    body = modify_request(body)

    client = request.app.state.client
    is_streaming = body.get("stream", False)
    qs = request.url.query or ""
    target_url = f"{config.target_api_base}/v1/messages"
    if qs:
        target_url += f"?{qs}"

    if is_streaming:
        req = client.build_request("POST", target_url, json=body, headers=headers)
        try:
            resp = await client.send(req, stream=True)
        except httpx.TimeoutException:
            return JSONResponse(
                content={"type": "error", "error": {"type": "timeout", "message": "upstream API timeout"}},
                status_code=504,
            )

        if resp.status_code >= 400:
            err_body = await resp.aread()
            await resp.aclose()
            try:
                err_data = json.loads(err_body)
            except json.JSONDecodeError:
                err_data = {"type": "error", "error": {"message": err_body.decode("utf-8", errors="replace")}}
            return JSONResponse(content=err_data, status_code=resp.status_code)

        return StreamingResponse(
            sse_modify_stream(resp),
            media_type="text/event-stream",
            status_code=resp.status_code,
            headers={"x-request-id": config.fake_request_id or resp.headers.get("x-request-id", "")},
        )

    try:
        resp = await client.post(target_url, json=body, headers=headers)
    except httpx.TimeoutException:
        return JSONResponse(
            content={"type": "error", "error": {"type": "timeout", "message": "upstream API timeout"}},
            status_code=504,
        )

    if resp.status_code >= 400:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    # 响应拦截: 改完再还客户端
    data = modify_response(resp.json())
    return JSONResponse(
        content=data,
        status_code=resp.status_code,
        headers={"x-request-id": config.fake_request_id or resp.headers.get("x-request-id", "")},
    )


@app.get("/v1/models")
async def proxy_models():
    models = [
        {
            "id": model_id,
            "type": "model",
            "display_name": model_id,
            "created_at": "2025-01-01T00:00:00Z",
        }
        for model_id in config.model_map.keys()
    ]
    return {"data": models}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.host, port=config.port)
