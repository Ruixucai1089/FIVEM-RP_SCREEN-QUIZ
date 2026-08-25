# 螢幕答題輔助工具

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
