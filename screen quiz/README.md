# 螢幕答題輔助工具（Windows）

此工具會擷取主螢幕、交由多模態模型找出選擇題答案與點擊座標，並自動點擊該位置。請僅用於已獲允許的練習、測試與自動化情境，並確認目標服務的規範。

## 安裝

請使用 Python 3.10 以上。在本資料夾開啟 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

設定 API 金鑰（關閉並重新開啟 PowerShell 後才會生效）：

```powershell
setx OPENAI_API_KEY "你的 OpenAI API 金鑰"
```

或使用 Gemini：

```powershell
setx GEMINI_API_KEY "你的 Gemini API 金鑰"
```

金鑰不會寫入程式或設定檔。若只想在目前這個 PowerShell 工作階段測試，可改用 `$env:OPENAI_API_KEY = "..."`。

## 執行

```powershell
python screen_quiz_helper.py
```

預設使用 Gemini 的 `gemini-3.5-flash-lite`。它支援圖片輸入與結構化輸出，並以低延遲、高頻率任務為目標；程式會使用最小思考層級以優先速度。若要改用 OpenAI，請在視窗切換供應商。

## 四選項快速版面

已針對你提供的「春風教育中心」四個水平選項畫面最佳化。程式只會傳送題目卡片區域，模型僅需回答第 1 至第 4 個選項；按鈕中心則由本機固定比例直接算出，因此不必等待模型定位座標。請保持瀏覽器縮放與題目視窗大小不變；若版面改變，先關閉自動點擊並校正後再使用。

- F2：擷取並辨識（全域熱鍵需要 `keyboard`；失敗時可讓程式視窗取得焦點後按 F2）。
- F10：停止目前的自動作答流程；不會再點選答案或開始下一堂。
- 「立即擷取」：與 F2 相同。
- 定時模式：預設每 7 秒擷取一次，且最低間隔為 7 秒。
- 自動點擊：預設開啟。關閉時會在視窗顯示建議座標，不會移動或點擊滑鼠。
- 自動接續下一題：預設開啟。按一次 F2 後，每次點選會先等待 1 秒，再輪詢題目卡片是否變更；找到下一題便立即繼續辨識，最多 12 題。若 10 秒內沒有新題目，會自動停止，避免重複點擊同一題。預設使用較低的變更門檻，能辨識題幹或選項文字的小幅變化。
- 本機題庫：高信心辨識的題目和答案會儲存在同資料夾的 `screen_quiz_helper_questions.sqlite3`。再次遇到相同畫面時，程式會優先使用本機答案，減少等待 API 的時間；逾時與低信心時的第一項備援答案不會儲存。
- 作答期限：每題擷取後最多等待模型 15 秒；超過時會直接選第一個答案並繼續下一題，避免 API 變慢使流程停住。
- API 配額不足：Gemini 回傳 429（配額已用盡）時，程式會停止且不會盲選第一項；請等待 Gemini 配額恢復，或改在工具視窗將供應商切換為 OpenAI。
- 自動接續下一堂：完成 10 題後，程式等待 46 秒（45 秒休息倒數結束後多等 1 秒），點「再上一堂」，選「讀書考試」，再點「開始上課」，並繼續下一輪。預設會持續進行，關閉程式即可停止。

## 安全設定與限制

程式會把題目卡片截圖壓縮成 JPEG，寬度最多 1280 px，以降低上傳與辨識時間；模型只回傳答案索引，點擊位置由本機依固定四選項版面計算。OpenAI 與 Gemini 每次請求預設最長等待 45 秒，連線逾時會自動重試一次；網路或 API 壅塞時仍可能更慢或失敗。模型信心低於門檻時會改選第一個答案並繼續；逾時或非四選項索引時不點擊。可用環境變數調整：

```powershell
$env:OPENAI_MODEL = "gpt-4o-mini"
$env:MIN_CONFIDENCE = "0.85"
$env:MAX_IMAGE_WIDTH = "1280"
$env:CAPTURE_INTERVAL = "7"
$env:REQUEST_TIMEOUT_SECONDS = "45"
$env:REQUEST_RETRY_ATTEMPTS = "2"
$env:ANSWER_DEADLINE_SECONDS = "15"
$env:OPENAI_IMAGE_DETAIL = "low"
$env:AUTO_CLICK = "1"
$env:FAST_FOUR_CHOICE_LAYOUT = "1"
$env:AUTO_ADVANCE = "1"
$env:AUTO_ADVANCE_MAX_QUESTIONS = "12"
$env:NEXT_QUESTION_TIMEOUT_SECONDS = "10"
$env:NEXT_QUESTION_INITIAL_DELAY_SECONDS = "1"
$env:NEXT_QUESTION_POLL_SECONDS = "0.2"
$env:NEXT_QUESTION_CHANGE_THRESHOLD = "0.03"
$env:LESSON_BREAK_SECONDS = "46"
$env:AUTO_LESSON_CYCLES = "0"  # 0 代表持續自動進行
```

`pyautogui.FAILSAFE` 已開啟：將游標移到螢幕角落可中止其動作。模型仍可能誤判題意或座標，第一次使用請保持自動點擊關閉，確認回傳結果與介面布局吻合後再開啟。

### 1600 × 900 視窗版面校正

若桌面是 1920 × 1080、遊戲內容是置中的 1600 × 900 視窗，程式預設會點擊四個按鈕中心的約 `(744, 534)`、`(888, 534)`、`(1032, 534)`、`(1176, 534)`。這些座標以你提供的畫面校正，與全螢幕比例無關。

若遊戲視窗移動或尺寸改變，可在啟動前設定可見遊戲內容（不含標題列）的左上角與大小：

```powershell
$env:GAME_VIEWPORT_LEFT = "160"
$env:GAME_VIEWPORT_TOP = "102"
$env:GAME_VIEWPORT_WIDTH = "1600"
$env:GAME_VIEWPORT_HEIGHT = "900"
```

若畫面含有個人資料、通知或機密內容，請先裁切/遮蔽再傳送；截圖會送至你選擇的 API 供應商。

若畫面顯示「錯誤」，請查看同一資料夾自動產生的 `screen_quiz_helper_error.log`。其中會記錄 API 狀態碼與完整錯誤原因，便於排除 API 金鑰、模型名稱或網路問題。

## 作業正誤判斷工具

`screen_true_false_helper.py` 是獨立工具，適用於畫面上方為題目、下方顯示「學生作答」，並有「正確」和「錯誤」兩個按鈕的版面。它會解題並判斷學生作答是否正確，點選對應按鈕。F2 開始連續處理 12 題；每輪完成後等待 46 秒，點「再上一堂」並確認畫面已切換（未切換時最多重試 3 次），等 1 秒點「開始上課」，並繼續下一輪。F10 停止；信心不足時會點選第一個按鈕「正確」，API 配額不足時不會點選。

```powershell
.\.venv312\Scripts\python.exe screen_true_false_helper.py
```
