"""The AI is hidden behind a small interface. To use another AI later (Claude, OpenAI),
write one more class with the same `complete` method. Nothing else changes."""
import json
import urllib.request
from abc import ABC, abstractmethod

from app.config import OLLAMA_MODEL, OLLAMA_URL


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str, json_schema: dict | None = None) -> str:
        """Send one question, get the text answer back."""


class OllamaClient(LLMClient):
    def __init__(self, model: str = OLLAMA_MODEL, url: str = OLLAMA_URL, timeout: int = 300):
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout

    def complete(self, system: str, user: str, json_schema: dict | None = None) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0},  # same question, same answer
        }
        if json_schema:
            body["format"] = json_schema  # forces the answer to match this shape
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())["message"]["content"]
        except OSError as e:
            raise RuntimeError(f"Could not reach Ollama at {self.url}. Is it running? ({e})") from e
