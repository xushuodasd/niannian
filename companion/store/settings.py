"""运行时设置持久化（网页里改的配置，重启后仍生效）。

只做一件事：把一份 JSON 存到磁盘 / 从磁盘读回。**不掺业务逻辑**，
业务侧（如 LLM 网关）只依赖它的 ``load`` / ``save`` 两个动作。

为什么单独一个文件而不是写回 ``config.toml``：
  * 写回 TOML 会丢注释、丢格式，难以保持"人读的配置文件"整洁；
  * 运行时设置（尤其是 API Key）与"随代码走的默认配置"本就该分离——
    config.toml 是**默认值**，settings.json 是**用户覆盖**；
  * settings.json 可加入 .gitignore，避免密钥进版本库。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger


class SettingsStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        """读取全部运行时设置；文件不存在或损坏都返回空字典（不阻塞启动）。"""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.warning(f"运行时设置读取失败，按空处理：{self._path}")
            return {}

    def save(self, data: dict[str, Any]) -> None:
        """整体覆盖写入（先写临时文件再替换，避免半截文件）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)