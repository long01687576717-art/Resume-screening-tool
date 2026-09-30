"""DeepSeek 调用封装：让 AI 按要求返回 JSON，并把结果缓存到本地。

缓存分两类存放（2026-10-01 用户决定）：
- .cache/resumes/：从简历提取的内容（含姓名、学校、经历），**30 天后自动删除**，网页上也可以随时手动清除
- .cache/jd/：从公开 JD 提取的内容，不含个人信息，长期保留（重建知识库时不用重新花钱调用）
"""
import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"
CACHE_GROUPS = ("resumes", "jd")
RESUME_CACHE_DAYS = 30
BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_RETRIES = 2


def read_env_file():
    """读取项目根目录 .env 文件中的配置（不依赖第三方库）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env(values):
    """更新 .env 中的指定项，保留文件里的其他内容（配置窗口和网页共用）。"""
    env_path = ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{key}={value}" for key, value in remaining.items()]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prune_resume_cache(days=RESUME_CACHE_DAYS):
    """删除超过保存期限的简历缓存（按写入时间算，读取不会延长期限）。返回删除的份数。"""
    folder = CACHE_DIR / "resumes"
    if not folder.exists():
        return 0
    deadline = time.time() - days * 86400
    old = [f for f in folder.glob("*.json") if f.stat().st_mtime < deadline]
    for f in old:
        f.unlink(missing_ok=True)
    return len(old)


def clear_resume_cache():
    """清除全部简历缓存（网页上的"清除简历缓存"按钮）。返回删除的份数。"""
    return prune_resume_cache(days=0) if (CACHE_DIR / "resumes").exists() else 0


class LLMClient:
    def __init__(self, use_cache=True, cache="resumes", api_key=None):
        """cache：缓存放在哪一类（resumes 简历，30 天删除；jd 公开 JD，长期保留）。
        api_key：网页里输入的 Key；不传时读 .env（优先）和系统环境变量。"""
        # .env 优先于系统环境变量：用配置窗口保存的 Key 总能生效
        env = {**os.environ, **read_env_file()}
        api_key = api_key or env.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("未找到 DEEPSEEK_API_KEY：请在网页侧边栏填写，或运行 python setup_key.py")
        assert cache in CACHE_GROUPS, cache
        self.cache_dir = CACHE_DIR / cache
        if cache == "resumes":
            prune_resume_cache()
        self.model = env.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        # 推理模型默认先"思考"再回答，信息提取用不到，关闭后速度快约 6 倍（实测 33 秒 → 5.5 秒）
        self.thinking = env.get("DEEPSEEK_THINKING", "disabled")
        self.use_cache = use_cache
        # 并行调用时偶尔会遇到限流、超时、连接中断等临时错误，由 SDK 自动重试（等待时间逐步加长）
        self._client = OpenAI(api_key=api_key, base_url=BASE_URL, max_retries=5)
        self._lock = threading.Lock()
        self._calls = {}  # 提示词 + 简历 → 调用结果（Future），同样的调用只做一次

    def extract_json(self, system_prompt, user_prompt):
        """调用 AI 并返回解析后的 JSON（dict）。
        几个模块用同一个提示词分析同一份简历时（如实习、校园经历、素质画像），只真正调用一次：
        先到的模块负责调用，其他模块等它的结果；本次运行内的结果也保存在内存里，不用缓存时同样有效。"""
        key = self._cache_path(system_prompt, user_prompt).stem
        with self._lock:
            future = self._calls.get(key)
            owner = future is None
            if owner:
                future = self._calls[key] = Future()
        if not owner:
            return copy.deepcopy(future.result())
        try:
            data = self._extract(system_prompt, user_prompt)
        except BaseException as e:
            with self._lock:
                del self._calls[key]  # 失败的不保留，下次重新调用
            future.set_exception(e)
            raise
        future.set_result(data)
        return copy.deepcopy(data)

    def _extract(self, system_prompt, user_prompt):
        cache_file = self._cache_path(system_prompt, user_prompt)
        if self.use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        last_error = None
        for _ in range(MAX_RETRIES):
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                extra_body={"thinking": {"type": self.thinking}},
            )
            content = response.choices[0].message.content or ""
            try:
                data = json.loads(content)
                break
            except json.JSONDecodeError as e:
                last_error = e
        else:
            raise ValueError(f"AI 返回的内容不是有效的 JSON：{last_error}")

        if self.use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def _cache_path(self, system_prompt, user_prompt):
        digest = hashlib.sha256(f"{self.model}\n{self.thinking}\n{system_prompt}\n{user_prompt}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest[:32]}.json"
