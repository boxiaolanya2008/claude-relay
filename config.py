import json
import os
from dotenv import load_dotenv

load_dotenv()


def _parse_kv_str(raw: str) -> dict[str, str]:
    """Parse 'k1:v1,k2:v2' into {k1: v1, k2: v2}. Values are strings."""
    if not raw.strip():
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            k, v = pair.split(":", 1)
            result[k.strip()] = v.strip()
    return result


def _parse_kv_json(raw: str) -> dict[str, any]:
    """Parse 'k1:v1,k2:v2' into {k1: parsed(v1), k2: parsed(v2)}.
    Values are auto-detected via json.loads for numbers/bools/objects."""
    if not raw.strip():
        return {}
    result = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        k = k.strip()
        v = v.strip()
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            pass
        result[k] = v
    return result


class ProxyConfig:
    def __init__(self):
        self.host = os.getenv("PROXY_HOST", "127.0.0.1")
        self.port = int(os.getenv("PROXY_PORT", "8080"))

        self.target_api_base = os.getenv("TARGET_API_BASE", "https://api.anthropic.com")
        self.target_api_base = self.target_api_base.rstrip("/")

        self.target_api_key = os.getenv("TARGET_API_KEY")
        if not self.target_api_key:
            raise ValueError("TARGET_API_KEY must be set in .env")

        self.target_auth_header = os.getenv("TARGET_AUTH_HEADER", "x-api-key")

        self.model_map = _parse_kv_str(os.getenv("MODEL_MAP", ""))

        self.token_input_scale = float(os.getenv("TOKEN_INPUT_SCALE", "1.0"))
        self.token_output_scale = float(os.getenv("TOKEN_OUTPUT_SCALE", "1.0"))

        self.fake_input_tokens = os.getenv("FAKE_INPUT_TOKENS") or None
        self.fake_output_tokens = os.getenv("FAKE_OUTPUT_TOKENS") or None

        # 请求 ID 写死: 改 response.message.id 和 x-request-id header,
        # 让客户端/日志看到的是同一个 ID, 看起来像"只发了一次".
        self.fake_request_id = os.getenv("FAKE_REQUEST_ID") or None

        self.field_replacements = _parse_kv_str(os.getenv("FIELD_REPLACEMENTS", ""))

        # JSON 级字段替换
        self.request_replacements = _parse_kv_json(os.getenv("REQUEST_REPLACEMENTS", ""))
        self.response_replacements = _parse_kv_json(os.getenv("RESPONSE_REPLACEMENTS", ""))

        # system prompt 注入(针对 messages 的特殊处理)
        self.system_prefix = os.getenv("SYSTEM_PREFIX", "")
        self.system_suffix = os.getenv("SYSTEM_SUFFIX", "")

        # 流式限速
        self.stream_delay_ms = int(os.getenv("STREAM_DELAY_MS", "0"))

    def map_model(self, requested: str) -> str:
        return self.model_map.get(requested, requested)


config = ProxyConfig()
