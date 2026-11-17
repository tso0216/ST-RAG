"""評估。

- `questions/`：題目檔。`ptt_v2/` 是目前用的 50 題（停留經驗、移動經驗、找地點、找時間、比較各 10 題），
  `ptt_v2/calibration/` 是校準評審用的反向答案；`ptt/` 是舊的 25 題，標註規則不同，不再使用。
- `baseline/`：五個系統的比較實驗（命令列、讀題目、系統、評審、指標、提示詞、結果檔），說明見 `baseline/__init__.py`。
- `gate/`：相關判斷（gate）的比較，說明見 `gate/systems.py`。
- `judge/`：評審的可信度（JEV 重問、LLM 評審對照），說明見 `judge/__init__.py`。
- `casestudy/`：案例研究，用一題對照題逐步看 full 的檢索過程（時間硬過濾、對照），說明見 `casestudy/__init__.py`。
"""
