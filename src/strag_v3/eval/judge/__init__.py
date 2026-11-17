"""評審的可信度實驗：只用 full（baseline 的 `strag`）那份結果檔的 50 個答案，不重跑系統。

- JEV 重問：同一個判斷問 JEV n 次（預設 3），看是／否會不會翻、noul 飄多少、兩個答案層指標跟著動多少。
- LLM 對照：同樣的問法和 `state` 改問一般 LLM（`claude-sonnet-5-5`、effort medium，只回是／否），
  跟 baseline 評審檔（論文數字用的那一份 JEV 判斷）逐筆比：一致率、Cohen's κ、兩個指標各是多少。

評審檔存在 `runs/`，報告和逐筆的 CSV 存在 `result/`。命令列見 `__main__.py`。
"""
