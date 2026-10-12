"""Operator-facing question summary contract; never rewrite the full evidence body."""
import sys

HEADER = "【給 Operator 的摘要】"
DIVIDER = "---詳細---"
FORMAT = """【給 Operator 的摘要】
要你決定：<一句話>
選項：A …／B …（沒有選項就寫「無」）
建議：<一句話，含理由>
（可選）不回覆的影響：<一句話>
---詳細---
<完整內容，維持現有的證據要求>"""
EXAMPLE = """【給 Operator 的摘要】
要你決定：是否送出這份變更供審查？
選項：A 送出／B 暫緩
建議：選 A，測試已通過，可以交給 reviewer 檢查。
---詳細---
完整送出預覽、測試輸出與證據放在這裡。"""
GUIDANCE = ("Every ask must start with this summary block, followed by a standalone ---詳細--- line. "
            "Keep the summary to about 5 lines and at most 300 characters, in plain language Operator understands; "
            "no SHAs, paths or logs. Keep all required evidence and exact outward-send previews after the divider.\n"
            + FORMAT + "\nExample:\n" + EXAMPLE)
WARNING = "missing Operator summary block: start with 【給 Operator 的摘要】 and a standalone ---詳細--- line; question accepted"


def parse(body):
    """Return the opening block including its header, excluding the exact divider.

    Legacy, empty or unterminated blocks return None. CRLF is accepted, but
    indented/inline dividers and a header buried in evidence are not delimiters.
    Do not truncate or otherwise enforce editorial limits on stored content.
    """
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    if not lines or lines[0] != HEADER:
        return None
    try:
        end = lines.index(DIVIDER, 1)
    except ValueError:
        return None
    return "\n".join(lines[:end]).rstrip() if any(line.strip() for line in lines[1:end]) else None


def wake_note(body):
    """Use the decision line for structured questions, keeping legacy first lines."""
    block = parse(body)
    if block is None:
        return body.splitlines()[0]
    lines = [line.strip() for line in block.splitlines()[1:] if line.strip()]
    return next((line for line in lines if line.startswith("要你決定：")), lines[0])


def warn(body, *, emit=True):
    if parse(body) is None:
        if emit:
            print("warning: " + WARNING, file=sys.stderr)
        return [WARNING]
    return []
