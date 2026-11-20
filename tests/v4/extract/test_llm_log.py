"""--verbose 時印出 llm 的輸入和輸出。"""
import asyncio
import logging

from v4.extract import llm_log


def test_input_and_output_are_logged_and_a_repeated_system_prompt_is_printed_once(monkeypatch, caplog):
    async def fake(system, user, schema, model, effort=None, tools=None):
        return {"answer": f"回 {user}"}
    monkeypatch.setattr("service.llm.call", fake)
    caplog.set_level(logging.INFO, logger="v4.extract.llm_log")

    asyncio.run(llm_log.call("規則很長", "第一題", {}, "挑候選", "pick.md"))
    asyncio.run(llm_log.call("規則很長", "第二題", {}, "挑候選", "web_address.md", tools=["WebSearch"]))

    first_in, first_out, second_in, second_out = [r.getMessage() for r in caplog.records]
    no = int(first_in.split("#")[1].split(" ")[0])
    assert first_in == f"━━━━━ llm #{no} 輸入：挑候選｜prompts/pick.md ━━━━━\n[system]\n規則很長\n[user]\n第一題"
    assert first_out.startswith(f"━━━━━ llm #{no} 輸出（挑候選：") and '"answer": "回 第一題"' in first_out
    assert second_in == (f"━━━━━ llm #{no + 1} 輸入：挑候選｜prompts/web_address.md（工具：WebSearch） ━━━━━\n"
                         f"[system] 跟 llm #{no} 一樣（挑候選）\n[user]\n第二題")
    assert '"answer": "回 第二題"' in second_out


def test_nothing_is_formatted_without_verbose(monkeypatch, caplog):
    async def fake(system, user, schema, model, effort=None, tools=None):
        return "ok"
    monkeypatch.setattr("service.llm.call", fake)
    caplog.set_level(logging.WARNING, logger="v4.extract.llm_log")

    assert asyncio.run(llm_log.call("s", "u", None, "x", "x.md")) == "ok"
    assert caplog.records == []
