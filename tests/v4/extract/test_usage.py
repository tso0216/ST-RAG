"""各部分的 token 和時間：只記開著 `collect()` 時的、每一段記到自己的部分、結束時印的佔比。"""
from service import llm
from v4.extract import usage
from v4.extract.pipeline import duration


def spend(tokens: int) -> None:
    llm.record(llm.Usage(input_tokens=tokens, output_tokens=0, cost_usd=0.01))


def test_nothing_is_recorded_without_collect():
    with usage.part(usage.LINE):
        spend(100)   # 不會出錯，也沒有地方記


def test_each_part_gets_its_own_tokens_and_time():
    with usage.collect() as board:
        with usage.part(usage.LINE):
            spend(300)
        with usage.part(usage.PICK):
            spend(100)
        with usage.part(usage.MAP):
            pass

    assert (board[usage.LINE].tally.calls, board[usage.LINE].tally.input_tokens) == (1, 300)
    assert (board[usage.PICK].tally.calls, board[usage.PICK].tally.input_tokens) == (1, 100)
    assert board[usage.NOTE].tally.calls == 0 and board[usage.MAP].tally.calls == 0


def test_lines_show_the_share_of_tokens_and_time():
    board = {name: usage.Part() for name in usage.PARTS}
    board[usage.LINE].tally.add(llm.Usage(input_tokens=700, output_tokens=50, cost_usd=0.03))
    board[usage.LINE].seconds = 30
    board[usage.PLAN].tally.add(llm.Usage(input_tokens=240, output_tokens=10, cost_usd=0.01))
    board[usage.PLAN].seconds = 60
    board[usage.MAP].seconds = 10

    lines = usage.lines(board, duration)

    assert lines[1:] == [
        f"  {usage.LINE}：llm 1 次，token 750（75%），US$0.03，時間 30 秒（30%）",
        f"  {usage.NOTE}：llm 0 次，token 0（0%），US$0.00，時間 0 秒（0%）",
        f"  {usage.PLAN}：llm 1 次，token 250（25%），US$0.01，時間 1 分 00 秒（60%）",
        f"  {usage.PICK}：llm 0 次，token 0（0%），US$0.00，時間 0 秒（0%）",
        f"  {usage.WEB}：llm 0 次，token 0（0%），US$0.00，時間 0 秒（0%）",
        f"  {usage.MAP}：時間 10 秒（10%）",
        f"  {usage.GSI}：時間 0 秒（0%）",
        f"  {usage.WRITE}：時間 0 秒（0%）",
    ]
