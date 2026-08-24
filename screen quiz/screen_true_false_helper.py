"""Windows helper for marking displayed student answers as correct or incorrect."""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk
from typing import Any

import pyautogui
import requests
from PIL import Image, ImageChops, ImageStat

try:
    import keyboard
except ImportError:
    keyboard = None


RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_correct": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 120},
    },
    "required": ["is_correct", "confidence", "reason"],
}

PROMPT = """Read the screenshot. It shows one question and a student's submitted answer.
Solve the question yourself, compare it with the student's answer, and decide whether the
student answer is correct. Return only the requested JSON. If the question or answer is
not readable, use confidence below 0.60. Keep reason under 12 words."""


@dataclass
class Mark:
    is_correct: bool
    confidence: float
    reason: str


class VisionClient:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    def mark(self, image_b64: str) -> Mark:
        if self.provider == "OpenAI":
            data = self._openai(image_b64)
        else:
            data = self._gemini(image_b64)
        return Mark(**data)

    def _openai(self, image_b64: str) -> dict[str, Any]:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("找不到 OPENAI_API_KEY 環境變數。")
        body = {
            "model": self.model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": PROMPT},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}", "detail": "low"},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "answer_mark", "strict": True, "schema": RESULT_SCHEMA}},
            "max_output_tokens": 100,
        }
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {key}"}, json=body, timeout=30,
        )
        self._raise_for_response(response, "OpenAI")
        payload = response.json()
        text = payload.get("output_text")
        if not isinstance(text, str):
            raise RuntimeError("OpenAI 未回傳可用內容。")
        return self._parse(text, "OpenAI")

    def _gemini(self, image_b64: str) -> dict[str, Any]:
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("找不到 GEMINI_API_KEY 環境變數。")
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={key}"
        body = {"contents": [{"parts": [
            {"text": PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
        ]}], "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": RESULT_SCHEMA,
            "maxOutputTokens": 100,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        }}
        response = requests.post(endpoint, json=body, timeout=30)
        self._raise_for_response(response, "Gemini")
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini 未回傳可用內容。") from exc
        return self._parse(text, "Gemini")

    @staticmethod
    def _parse(text: str, provider: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{provider} 回傳的內容不是有效 JSON。") from exc

    @staticmethod
    def _raise_for_response(response: requests.Response, provider: str) -> None:
        if response.ok:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        if response.status_code == 429:
            raise RuntimeError(f"{provider} API 配額已用盡（429）；不會自動點選。")
        raise RuntimeError(f"{provider} API 錯誤 {response.status_code}: {detail}")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("作業正誤判斷工具")
        self.root.resizable(False, False)
        self.busy = False
        self.stop_requested = threading.Event()
        self.events: queue.Queue[str] = queue.Queue()

        self.provider = tk.StringVar(value=os.getenv("VISION_PROVIDER", "Gemini"))
        self.model = tk.StringVar(value=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        self.question_count = tk.IntVar(value=int(os.getenv("MARKING_MAX_QUESTIONS", "12")))
        self.auto_click = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="待機：按 F2 開始")
        self.details = tk.StringVar(value="F10 可隨時停止")

        frame = ttk.Frame(root, padding=14)
        frame.grid()
        ttk.Label(frame, text="供應商").grid(row=0, column=0, sticky="w")
        picker = ttk.Combobox(frame, textvariable=self.provider, values=("Gemini", "OpenAI"), state="readonly", width=12)
        picker.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        picker.bind("<<ComboboxSelected>>", self._provider_changed)
        ttk.Label(frame, text="模型").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.model, width=30).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(frame, text="題數").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(frame, textvariable=self.question_count, values=(8, 10, 12, 14), state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(frame, text="判斷後自動點選", variable=self.auto_click).grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(frame, text="開始判斷（F2）", command=self.capture).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(frame, textvariable=self.status, foreground="#1f4e79", wraplength=360).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(frame, textvariable=self.details, wraplength=360).grid(row=6, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(frame, text="此程式由超帥Rui製作 ", font=("TkDefaultFont", 10)).grid(row=7, column=0, columnspan=2, pady=(10, 0))
        root.bind("<F2>", lambda _: self.capture())
        root.bind("<F10>", lambda _: self.request_stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll_events)

        if keyboard:
            try:
                keyboard.add_hotkey("f2", lambda: self.events.put("start"))
                keyboard.add_hotkey("f10", lambda: self.events.put("stop"))
                self.details.set("F2 開始；F10 停止")
            except Exception:
                self.details.set("請讓本視窗取得焦點後按 F2／F10")

    def _provider_changed(self, _event: Any = None) -> None:
        self.model.set("gpt-4o-mini" if self.provider.get() == "OpenAI" else "gemini-3.5-flash-lite")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self.request_stop() if event == "stop" else self.capture()
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def capture(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.stop_requested.clear()
        self.status.set("辨識中…")
        threading.Thread(
            target=self._run,
            args=(self.provider.get(), self.model.get().strip(), self.auto_click.get(), self.question_count.get()),
            daemon=True,
        ).start()

    def request_stop(self) -> None:
        self.stop_requested.set()
        if self.busy:
            self.status.set("正在停止…")
            self.details.set("不會再點選正確或錯誤")

    def _run(self, provider: str, model: str, auto_click: bool, total: int) -> None:
        try:
            max_lessons = max(0, int(os.getenv("MARKING_MAX_LESSONS", "0")))  # 0 means continuous.
            lesson_number = 1
            while max_lessons == 0 or lesson_number <= max_lessons:
                image: Image.Image | None = None
                for number in range(1, total + 1):
                    if self.stop_requested.is_set():
                        return self._finish_async("已停止", "已由 F10 取消。")
                    image = image or pyautogui.screenshot()
                    fingerprint = self._fingerprint(image)
                    marked_image = self._crop(image)
                    answer = VisionClient(provider, model).mark(self._encode(marked_image))
                    if self.stop_requested.is_set():
                        return self._finish_async("已停止", "已由 F10 取消。")
                    if answer.confidence < float(os.getenv("MARKING_MIN_CONFIDENCE", "0.75")):
                        # The first button in this layout is 「正確」.
                        answer = Mark(True, answer.confidence, "信心不足時依設定選第一個按鈕")
                    label = "正確" if answer.is_correct else "錯誤"
                    if not auto_click:
                        return self._finish_async("已判斷（未點選）", f"第 {lesson_number} 堂／第 {number} 題：{label}；{answer.reason}")
                    x, y = self._button_position(answer.is_correct, image.size)
                    pyautogui.click(x, y)
                    self.root.after(0, lambda n=number, name=label: self._show_progress(n, name))
                    if number == total:
                        continue
                    image = self._wait_for_next(fingerprint)
                    if image is None:
                        state = "已停止" if self.stop_requested.is_set() else "已點選"
                        detail = "已由 F10 取消。" if self.stop_requested.is_set() else "未偵測到下一題。"
                        return self._finish_async(state, detail)

                if max_lessons and lesson_number >= max_lessons:
                    return self._finish_async("已完成", f"已連續判斷 {lesson_number} 堂課。")
                self.root.after(0, lambda n=lesson_number: self._show_break(n))
                if not self._start_next_lesson():
                    if self.stop_requested.is_set():
                        return self._finish_async("已停止", "已由 F10 取消。")
                    return self._finish_async("未開始下一堂", "「再上一堂」未成功切換畫面，已停止。")
                lesson_number += 1
        except Exception as exc:
            self._finish_async("錯誤", str(exc))

    @staticmethod
    def _crop(image: Image.Image) -> Image.Image:
        width, height = image.size
        return image.crop((round(width * .25), round(height * .25), round(width * .75), round(height * .60)))

    @staticmethod
    def _encode(image: Image.Image) -> str:
        if image.width > 1280:
            image = image.resize((1280, round(image.height * 1280 / image.width)), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @classmethod
    def _fingerprint(cls, image: Image.Image) -> Image.Image:
        return cls._crop(image).convert("L").resize((48, 24), Image.Resampling.BILINEAR)

    def _wait_for_next(self, before: Image.Image) -> Image.Image | None:
        if self.stop_requested.wait(float(os.getenv("NEXT_QUESTION_INITIAL_DELAY_SECONDS", "1"))):
            return None
        deadline = time.monotonic() + float(os.getenv("NEXT_QUESTION_TIMEOUT_SECONDS", "10"))
        while time.monotonic() < deadline:
            current = pyautogui.screenshot()
            score = ImageStat.Stat(ImageChops.difference(before, self._fingerprint(current))).mean[0]
            if score >= float(os.getenv("NEXT_QUESTION_CHANGE_THRESHOLD", "0.03")):
                return current
            if self.stop_requested.wait(float(os.getenv("NEXT_QUESTION_POLL_SECONDS", "0.2"))):
                return None
        return None

    @staticmethod
    def _button_position(is_correct: bool, screen_size: tuple[int, int]) -> tuple[int, int]:
        # Button centers calibrated from the supplied 1920x1080 / 1600x900 game view.
        screen_width, _screen_height = screen_size
        viewport_width = int(os.getenv("GAME_VIEWPORT_WIDTH", "1600"))
        viewport_left = int(os.getenv("GAME_VIEWPORT_LEFT", str(round((screen_width - viewport_width) / 2))))
        viewport_top = int(os.getenv("GAME_VIEWPORT_TOP", "102"))
        x_ratio = .455 if is_correct else .545
        return round(viewport_left + viewport_width * x_ratio), round(viewport_top + 900 * .520)

    def _show_progress(self, number: int, label: str) -> None:
        self.status.set(f"第 {number} 題：已點選{label}")
        self.details.set("正在偵測下一題…")

    def _show_break(self, lesson_number: int) -> None:
        self.status.set(f"第 {lesson_number} 堂已完成")
        self.details.set("休息中；46 秒後自動開始下一堂…")

    def _start_next_lesson(self) -> bool:
        """Wait for the break, then click Next Lesson and Start Lesson."""
        if self.stop_requested.wait(float(os.getenv("LESSON_BREAK_SECONDS", "46"))):
            return False
        # Button centers calibrated from the supplied 1920x1080 / 1600x900 game view.
        if not self._click_until_screen_changes(
            int(os.getenv("NEXT_LESSON_X", "1008")),
            int(os.getenv("NEXT_LESSON_Y", "769")),
        ):
            return False
        if self.stop_requested.wait(float(os.getenv("START_LESSON_DELAY_SECONDS", "1"))):
            return False
        pyautogui.click(
            int(os.getenv("START_LESSON_X", "960")),
            int(os.getenv("START_LESSON_Y", "751")),
        )
        return not self.stop_requested.is_set()

    def _click_until_screen_changes(self, x: int, y: int) -> bool:
        """Click a transition button again only if its screen has not changed yet."""
        retries = max(1, int(os.getenv("NEXT_LESSON_CLICK_RETRIES", "3")))
        wait_seconds = float(os.getenv("NEXT_LESSON_CONFIRM_SECONDS", "0.8"))
        before = self._fingerprint(pyautogui.screenshot())
        for attempt in range(1, retries + 1):
            if self.stop_requested.is_set():
                return False
            pyautogui.click(x, y)
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if self.stop_requested.wait(0.1):
                    return False
                current = pyautogui.screenshot()
                score = ImageStat.Stat(ImageChops.difference(before, self._fingerprint(current))).mean[0]
                if score >= float(os.getenv("NEXT_LESSON_CHANGE_THRESHOLD", "1")):
                    return True
        return False

    def _finish_async(self, state: str, detail: str) -> None:
        self.root.after(0, lambda: self._finish(state, detail))

    def _finish(self, state: str, detail: str) -> None:
        self.busy = False
        self.status.set(state)
        self.details.set(detail)

    def close(self) -> None:
        self.stop_requested.set()
        if keyboard:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = .02
    window = tk.Tk()
    App(window)
    window.mainloop()
