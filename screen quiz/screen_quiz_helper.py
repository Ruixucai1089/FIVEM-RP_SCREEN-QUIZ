"""A small Windows screen-question helper with OpenAI and Gemini vision support."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Any

import pyautogui
import requests
from PIL import Image, ImageChops, ImageStat

try:
    import keyboard  # Global F2 hotkey on Windows.
except ImportError:
    keyboard = None


ERROR_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_quiz_helper_error.log")
QUESTION_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_quiz_helper_questions.sqlite3")
logging.basicConfig(
    filename=ERROR_LOG_PATH,
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("screen_quiz_helper")


RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "choice_index": {"type": "integer", "minimum": 0, "maximum": 20},
        "choice_text": {"type": "string"},
        "question_text": {"type": "string", "maxLength": 300},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 120},
    },
    "required": ["choice_index", "choice_text", "question_text", "confidence", "reason"],
}

PROMPT = """Read this screenshot. It contains one multiple-choice question and its answer options.
Solve it and return only the schema JSON. Include the question text without the answer options.
choice_index is zero-based (0 to 3, left to right).
If unclear, use confidence below 0.60. Keep reason under 12 words."""


@dataclass
class Answer:
    choice_index: int
    choice_text: str
    confidence: float
    reason: str
    question_text: str = ""


class QuestionCache:
    """Small local cache for answers to visually identical quiz cards."""

    def __init__(self, path: str = QUESTION_DB_PATH) -> None:
        self.path = path
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS question_cache (
                    image_hash TEXT PRIMARY KEY,
                    question_text TEXT NOT NULL,
                    choice_index INTEGER NOT NULL,
                    choice_text TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    def lookup(self, image_hash: str) -> Answer | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT question_text, choice_index, choice_text, confidence FROM question_cache WHERE image_hash = ?",
                (image_hash,),
            ).fetchone()
        if row is None:
            return None
        question_text, choice_index, choice_text, confidence = row
        return Answer(choice_index, choice_text, confidence, "本機題庫", question_text)

    def save(self, image_hash: str, answer: Answer) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO question_cache
                    (image_hash, question_text, choice_index, choice_text, confidence)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(image_hash) DO UPDATE SET
                        question_text = excluded.question_text,
                        choice_index = excluded.choice_index,
                        choice_text = excluded.choice_text,
                        confidence = excluded.confidence,
                        updated_at = CURRENT_TIMESTAMP""",
                (image_hash, answer.question_text, answer.choice_index, answer.choice_text, answer.confidence),
            )


class VisionClient:
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    def solve(self, image_b64: str) -> Answer:
        LOGGER.info("Sending recognition request to %s model %s", self.provider, self.model)
        if self.provider == "OpenAI":
            data = self._openai(image_b64)
        else:
            data = self._gemini(image_b64)
        return Answer(**data)

    def _openai(self, image_b64: str) -> dict[str, Any]:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("找不到 OPENAI_API_KEY 環境變數。")
        body = {
            "model": self.model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": PROMPT},
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}", "detail": os.getenv("OPENAI_IMAGE_DETAIL", "low")},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "screen_answer", "strict": True, "schema": RESULT_SCHEMA}},
            "max_output_tokens": 140,
        }
        response = self._post_with_retry(
            "https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {key}"},
            json_body=body,
        )
        self._raise_for_response(response, "OpenAI")
        payload = response.json()
        output = payload.get("output_text") or self._find_output_text(payload)
        if not output:
            status = payload.get("status", "unknown")
            raise RuntimeError(f"OpenAI 未回傳結構化文字（狀態：{status}）。")
        try:
            return json.loads(output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenAI 回傳的內容不是有效 JSON：{output!r}") from exc

    @staticmethod
    def _find_output_text(payload: dict[str, Any]) -> str | None:
        """Support both convenience output_text and raw Responses API content shapes."""
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "refusal"}:
                    text = content.get("text") or content.get("refusal")
                    if isinstance(text, str):
                        return text
        return None

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
            "maxOutputTokens": 140,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        }}
        response = self._post_with_retry(endpoint, json_body=body)
        self._raise_for_response(response, "Gemini")
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Gemini 未回傳可用內容。") from exc
        try:
            return json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            # Gemini can occasionally produce malformed JSON despite the schema.
            # Raise a clear error so the normal fallback flow can continue.
            raise RuntimeError(f"Gemini 回傳的內容不是有效 JSON：{text!r}") from exc

    @staticmethod
    def _post_with_retry(
        url: str, *, headers: dict[str, str] | None = None, json_body: dict[str, Any]
    ) -> requests.Response:
        """Retry only transient network failures; API error responses are handled normally."""
        attempts = max(1, int(os.getenv("REQUEST_RETRY_ATTEMPTS", "2")))
        timeout = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "45"))
        for attempt in range(1, attempts + 1):
            try:
                return requests.post(url, headers=headers, json=json_body, timeout=timeout)
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise RuntimeError(
                        f"連線逾時或中斷（已嘗試 {attempts} 次，每次最長 {timeout:.0f} 秒）：{exc}"
                    ) from exc
                LOGGER.warning("Request attempt %s/%s failed: %s", attempt, attempts, exc)
                time.sleep(1)
        raise RuntimeError("無法建立 API 連線。")  # Unreachable; keeps type checkers satisfied.

    @staticmethod
    def _raise_for_response(response: requests.Response, provider: str) -> None:
        if response.ok:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        if not detail:
            detail = response.text or "（API 未提供錯誤內容）"
        raise RuntimeError(f"{provider} API 錯誤 {response.status_code}: {detail}")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("螢幕答題輔助工具")
        self.root.resizable(False, False)
        self.busy = False
        self.timer_id: str | None = None
        self.hotkey_events: queue.Queue[None] = queue.Queue()
        self.stop_events: queue.Queue[None] = queue.Queue()
        self.stop_requested = threading.Event()
        self.question_cache = QuestionCache()

        self.provider = tk.StringVar(value=os.getenv("VISION_PROVIDER", "Gemini"))
        self.model = tk.StringVar(value=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
        self.interval = tk.IntVar(value=int(os.getenv("CAPTURE_INTERVAL", "7")))
        self.auto_click = tk.BooleanVar(value=os.getenv("AUTO_CLICK", "1") == "1")
        self.auto_advance = tk.BooleanVar(value=os.getenv("AUTO_ADVANCE", "1") == "1")
        self.fast_layout = tk.BooleanVar(value=os.getenv("FAST_FOUR_CHOICE_LAYOUT", "1") == "1")
        self.status = tk.StringVar(value="待機：按 F2 擷取並辨識")
        self.details = tk.StringVar(value="7 秒模式可用；自動點擊已啟用")

        frame = ttk.Frame(root, padding=14)
        frame.grid()
        ttk.Label(frame, text="供應商").grid(row=0, column=0, sticky="w")
        picker = ttk.Combobox(frame, textvariable=self.provider, values=("OpenAI", "Gemini"), state="readonly", width=12)
        picker.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        picker.bind("<<ComboboxSelected>>", self._provider_changed)
        ttk.Label(frame, text="模型").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.model, width=30).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(frame, text="辨識成功後自動點擊", variable=self.auto_click).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(frame, text="四選項快速版面（建議開啟）", variable=self.fast_layout).grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frame, text="點選後自動接續下一題（最多 12 題）", variable=self.auto_advance).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Button(frame, text="立即擷取（F2）", command=self.capture).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(frame, text="定時秒數").grid(row=6, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(frame, from_=7, to=3600, textvariable=self.interval, width=8).grid(row=6, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        self.timer_button = ttk.Button(frame, text="開始定時模式", command=self.toggle_timer)
        self.timer_button.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Separator(frame).grid(row=8, column=0, columnspan=2, sticky="ew", pady=12)
        ttk.Label(frame, textvariable=self.status, foreground="#1f4e79", wraplength=340).grid(row=9, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, textvariable=self.details, wraplength=340).grid(row=10, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(frame, text="此程式由超帥Rui製作", font=("TkDefaultFont", 10)).grid(row=11, column=0, columnspan=2, pady=(10, 0))
        root.bind("<F2>", lambda _: self.capture())
        root.bind("<F10>", lambda _: self.request_stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._poll_hotkey)

        if keyboard:
            try:
                keyboard.add_hotkey("f2", lambda: self.hotkey_events.put(None))
                keyboard.add_hotkey("f10", lambda: self.stop_events.put(None))
                self.details.set("F2 開始；F10 停止；7 秒模式可用")
            except Exception:
                self.details.set("F2 僅在本視窗取得焦點時可用")

    def _provider_changed(self, _event: Any = None) -> None:
        self.model.set("gpt-4o-mini" if self.provider.get() == "OpenAI" else "gemini-3.5-flash-lite")

    def capture(self) -> None:
        if self.busy:
            return
        self.stop_requested.clear()
        self.busy = True
        LOGGER.info("Capture started")
        self.status.set("辨識中…")
        self.details.set("正在擷取主螢幕並送出圖片")
        # Snapshot UI values in the Tk main thread before starting background work.
        threading.Thread(
            target=self._run_capture,
            args=(self.provider.get(), self.model.get().strip(), self.auto_click.get(), self.fast_layout.get(), self.auto_advance.get()),
            daemon=True,
        ).start()

    def _poll_hotkey(self) -> None:
        try:
            self.stop_events.get_nowait()
            self.request_stop()
        except queue.Empty:
            pass
        try:
            self.hotkey_events.get_nowait()
            self.capture()
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_hotkey)

    def request_stop(self) -> None:
        """Cancel the active run before it can make another click."""
        if not self.busy:
            self.status.set("待機：沒有進行中的作答")
            return
        self.stop_requested.set()
        self.status.set("正在停止…")
        self.details.set("不會再點選答案或開始下一堂")

    def _run_capture(
        self, provider: str, model: str, auto_click: bool, fast_layout: bool, auto_advance: bool
    ) -> None:
        started = time.monotonic()
        try:
            max_questions = max(1, int(os.getenv("AUTO_ADVANCE_MAX_QUESTIONS", "12")))
            max_lessons = max(0, int(os.getenv("AUTO_LESSON_CYCLES", "0")))  # 0 means continuous.
            lesson_number = 1
            while max_lessons == 0 or lesson_number <= max_lessons:
                image: Image.Image | None = None
                for question_number in range(1, max_questions + 1):
                    if self.stop_requested.is_set():
                        self.root.after(0, lambda: self._finish("已停止", "已由 F10 取消作答。"))
                        return
                    image = image or pyautogui.screenshot()  # Primary display in normal Windows configurations.
                    original_size = image.size
                    question_fingerprint = self._question_fingerprint(image)
                    image_for_model = self._quiz_crop(image) if fast_layout else image
                    question_hash = self._question_hash(image)
                    answer = self.question_cache.lookup(question_hash)
                    if answer is None:
                        encoded, _scale_x, _scale_y = self._encode_image(image_for_model)
                        answer = self._solve_with_deadline(provider, model, encoded)
                    else:
                        LOGGER.info("Answered from local question cache: %s", answer.question_text)
                    if self.stop_requested.is_set():
                        self.root.after(0, lambda: self._finish("已停止", "已由 F10 取消作答。"))
                        return
                    self._validate(answer)
                    answer = self._fallback_to_first_choice(answer)
                    if answer.reason not in {"本機題庫", "辨識逾時，依設定選第一項", "信心不足時依設定選第一項"}:
                        self.question_cache.save(question_hash, answer)
                        LOGGER.info("Saved high-confidence answer to local question cache: %s", answer.question_text)
                    x, y = self._choice_position(answer.choice_index, original_size)
                    elapsed = time.monotonic() - started
                    summary = (
                        f"第 {lesson_number} 堂／第 {question_number} 題：{answer.choice_text}（第 {answer.choice_index + 1} 項，"
                        f"信心 {answer.confidence:.0%}，耗時 {elapsed:.2f} 秒）"
                    )
                    if not auto_click:
                        LOGGER.info("Recognized choice %s without clicking in %.2f seconds", answer.choice_index + 1, elapsed)
                        self.root.after(0, lambda: self._finish("已辨識（未點擊）", f"{summary}；座標 ({x}, {y})"))
                        return

                    pyautogui.moveTo(x, y)
                    pyautogui.click()
                    LOGGER.info("Clicked lesson %s question %s choice %s at (%s, %s) in %.2f seconds", lesson_number, question_number, answer.choice_index + 1, x, y, elapsed)
                    if not auto_advance:
                        self.root.after(0, lambda: self._finish("已點擊", summary))
                        return
                    if question_number == max_questions:
                        # The last answer leads to the completion screen, not another question.
                        continue
                    self.root.after(0, lambda n=question_number: self._show_waiting_for_next(n))
                    image = self._wait_for_next_question(question_fingerprint)
                    if image is None:
                        if self.stop_requested.is_set():
                            self.root.after(0, lambda: self._finish("已停止", "已由 F10 取消作答。"))
                            return
                        self.root.after(0, lambda: self._finish("已點擊", f"{summary}；未偵測到下一題，已停止。"))
                        return

                if max_lessons and lesson_number >= max_lessons:
                    self.root.after(0, lambda: self._finish("已完成", f"已連續處理 {lesson_number} 堂課。"))
                    return
                self.root.after(0, lambda n=lesson_number: self._show_break(n))
                if not self._start_next_lesson():
                    if self.stop_requested.is_set():
                        self.root.after(0, lambda: self._finish("已停止", "已由 F10 取消作答。"))
                        return
                    self.root.after(0, lambda: self._finish("已完成", f"第 {lesson_number} 堂課已完成；無法開始下一堂。"))
                    return
                lesson_number += 1
        except Exception as exc:
            error_text = str(exc).strip()
            if not error_text or error_text == "None":
                error_text = f"{type(exc).__name__}: {exc!r}"
            self._write_error_log(error_text)
            self.root.after(0, lambda: self._finish("錯誤", error_text))

    @staticmethod
    def _write_error_log(error_text: str) -> None:
        try:
            LOGGER.error("%s\n%s", error_text, traceback.format_exc())
        except Exception:
            pass

    @staticmethod
    def _encode_image(image: Image.Image) -> tuple[str, float, float]:
        max_width = int(os.getenv("MAX_IMAGE_WIDTH", "1280"))
        width, height = image.size
        if width > max_width:
            new_height = round(height * max_width / width)
            image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii"), width / image.width, height / image.height

    @staticmethod
    def _question_fingerprint(image: Image.Image) -> Image.Image:
        """Create a small comparison image for detecting when the next question appears."""
        return App._quiz_crop(image).convert("L").resize((48, 24), Image.Resampling.BILINEAR)

    @staticmethod
    def _question_hash(image: Image.Image) -> str:
        """Return a stable visual key for the question card and its four choices."""
        fingerprint = App._quiz_crop(image).convert("L").resize((64, 32), Image.Resampling.LANCZOS)
        # A tiny brightness threshold makes the key resilient to small rendering differences.
        pixels = bytes(255 if pixel >= 180 else 0 for pixel in fingerprint.getdata())
        return base64.b16encode(pixels).decode("ascii")

    @staticmethod
    def _has_new_question(before: Image.Image, current: Image.Image) -> bool:
        difference = ImageChops.difference(before, App._question_fingerprint(current))
        # Question text and options can change without moving the card itself.
        # On the small comparison image, a text-only update can be below 0.1,
        # so the former threshold of 7 incorrectly treated the new question as
        # the old one.
        change_score = ImageStat.Stat(difference).mean[0]
        threshold = float(os.getenv("NEXT_QUESTION_CHANGE_THRESHOLD", "0.03"))
        LOGGER.info("Next-question change score: %.2f (threshold %.2f)", change_score, threshold)
        return change_score >= threshold

    def _wait_for_next_question(self, before: Image.Image) -> Image.Image | None:
        """Wait briefly for a changed quiz card so the same answer is never clicked twice."""
        timeout = float(os.getenv("NEXT_QUESTION_TIMEOUT_SECONDS", "10"))
        # Keep the requested one-second delay, then poll quickly until the card changes.
        initial_delay = float(os.getenv("NEXT_QUESTION_INITIAL_DELAY_SECONDS", "1"))
        poll_interval = float(os.getenv("NEXT_QUESTION_POLL_SECONDS", "0.2"))
        if self.stop_requested.wait(initial_delay):
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = pyautogui.screenshot()
            if self._has_new_question(before, current):
                return current
            if self.stop_requested.wait(poll_interval):
                return None
        return None

    def _show_waiting_for_next(self, question_number: int) -> None:
        self.status.set(f"第 {question_number} 題已點擊")
        self.details.set("正在偵測下一題…")

    def _show_break(self, lesson_number: int) -> None:
        self.status.set(f"第 {lesson_number} 堂已完成")
        self.details.set("休息中；等待後將自動開始下一堂…")

    @staticmethod
    def _fallback_to_first_choice(answer: Answer) -> Answer:
        """Use the first answer when model confidence is too low to choose reliably."""
        if answer.reason == "辨識逾時，依設定選第一項":
            return answer
        minimum_confidence = float(os.getenv("MIN_CONFIDENCE", "0.75"))
        if answer.confidence >= minimum_confidence:
            return answer
        LOGGER.warning(
            "Low confidence (%.0f%%); falling back to the first choice instead of stopping.",
            answer.confidence * 100,
        )
        return Answer(
            choice_index=0,
            choice_text="第一個答案（模型信心不足）",
            confidence=answer.confidence,
            reason="信心不足時依設定選第一項",
        )

    def _solve_with_deadline(self, provider: str, model: str, image_b64: str) -> Answer:
        """Return a first-choice fallback if recognition has not finished in time."""
        deadline = float(os.getenv("ANSWER_DEADLINE_SECONDS", "15"))
        result: queue.Queue[Answer | BaseException] = queue.Queue(maxsize=1)

        def solve() -> None:
            try:
                result.put(VisionClient(provider, model).solve(image_b64))
            except BaseException as exc:
                result.put(exc)

        threading.Thread(target=solve, daemon=True).start()
        deadline_at = time.monotonic() + deadline
        while time.monotonic() < deadline_at:
            if self.stop_requested.is_set():
                return Answer(0, "第一個答案（已停止）", 0.0, "已停止")
            try:
                outcome = result.get(timeout=min(0.1, deadline_at - time.monotonic()))
            except queue.Empty:
                continue
            if isinstance(outcome, BaseException):
                if "API 錯誤 429" in str(outcome):
                    # Quota exhaustion is not an uncertain answer.  Do not
                    # turn it into a blind click on the first option.
                    raise RuntimeError(
                        "Gemini API 配額已用盡；請等待服務恢復、改用其他模型，或切換至 OpenAI。"
                    ) from outcome
                LOGGER.warning("Recognition failed; selecting the first choice: %s", outcome)
                return Answer(0, "第一個答案（辨識錯誤）", 0.0, "辨識錯誤，依設定選第一項")
            return outcome
        LOGGER.warning("Recognition exceeded %.1f seconds; selecting the first choice.", deadline)
        return Answer(0, "第一個答案（辨識逾時）", 0.0, "辨識逾時，依設定選第一項")

    @staticmethod
    def _click_configured_position(x_name: str, y_name: str, default_x: int, default_y: int) -> None:
        pyautogui.click(
            int(os.getenv(x_name, str(default_x))),
            int(os.getenv(y_name, str(default_y))),
        )

    def _start_next_lesson(self) -> bool:
        """Wait for the break, then select Study Exam and start the next lesson."""
        # The on-screen break is 45 seconds; wait one extra second before clicking.
        break_seconds = float(os.getenv("LESSON_BREAK_SECONDS", "46"))
        if self.stop_requested.wait(break_seconds):
            return False

        # Measured from the supplied 1920 x 1080 screenshots.
        self._click_configured_position("NEXT_LESSON_X", "NEXT_LESSON_Y", 1008, 769)
        if self.stop_requested.wait(float(os.getenv("NEXT_LESSON_OPEN_DELAY_SECONDS", "1"))):
            return False
        self._click_configured_position("STUDY_EXAM_X", "STUDY_EXAM_Y", 1100, 640)
        if self.stop_requested.wait(float(os.getenv("MODE_SELECT_DELAY_SECONDS", "0.5"))):
            return False
        self._click_configured_position("START_LESSON_X", "START_LESSON_Y", 960, 751)
        if self.stop_requested.wait(float(os.getenv("START_LESSON_DELAY_SECONDS", "1"))):
            return False
        return True

    @staticmethod
    def _validate(answer: Answer) -> None:
        fields = (answer.choice_index, answer.confidence)
        if not all(isinstance(value, (int, float)) for value in fields):
            raise ValueError("API 回傳的選項或信心值格式不正確。")
        if not 0 <= answer.choice_index <= 3:
            raise ValueError("此快速版面只接受四個選項（索引 0 至 3）。")

    @staticmethod
    def _quiz_crop(image: Image.Image) -> Image.Image:
        """Crop the question card seen in the supplied four-choice game layout."""
        width, height = image.size
        left, top, right, bottom = (0.05, 0.20, 0.95, 0.63)
        return image.crop((round(width * left), round(height * top), round(width * right), round(height * bottom)))

    @staticmethod
    def _choice_position(choice_index: int, size: tuple[int, int]) -> tuple[int, int]:
        """Return centers for the four buttons inside the game viewport.

        The quiz runs in a 1600 x 900 game viewport centered horizontally in a
        1920 x 1080 desktop.  It must not use desktop-relative coordinates:
        doing that makes every click land too far left when the game is windowed.
        The environment variables keep this adjustable if the game window moves.
        """
        screen_width, screen_height = size
        viewport_width = int(os.getenv("GAME_VIEWPORT_WIDTH", "1600"))
        viewport_height = int(os.getenv("GAME_VIEWPORT_HEIGHT", "900"))
        viewport_left = int(os.getenv(
            "GAME_VIEWPORT_LEFT", str(round((screen_width - viewport_width) / 2))
        ))
        # 102 is the visible game-content top in the supplied 1920 x 1080 screenshot.
        viewport_top = int(os.getenv("GAME_VIEWPORT_TOP", "102"))

        # Measured within the 1600 x 900 game content, left to right.
        x_ratios = (0.365, 0.455, 0.545, 0.635)
        y_ratio = 0.480
        return (
            round(viewport_left + viewport_width * x_ratios[choice_index]),
            round(viewport_top + viewport_height * y_ratio),
        )

    def _finish(self, state: str, detail: str) -> None:
        self.busy = False
        self.status.set(state)
        self.details.set(detail)

    def toggle_timer(self) -> None:
        if self.timer_id:
            self.root.after_cancel(self.timer_id)
            self.timer_id = None
            self.timer_button.config(text="開始定時模式")
            self.status.set("待機：定時模式已停止")
        else:
            self.timer_button.config(text="停止定時模式")
            self._timer_tick()

    def _timer_tick(self) -> None:
        self.capture()
        self.timer_id = self.root.after(max(7, self.interval.get()) * 1000, self._timer_tick)

    def close(self) -> None:
        if keyboard:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    LOGGER.info("Application started")
    pyautogui.FAILSAFE = True  # Move the pointer to a corner to abort PyAutoGUI actions.
    pyautogui.PAUSE = 0.02
    window = tk.Tk()
    App(window)
    window.mainloop()
