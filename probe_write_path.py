"""
写入链路验证：session_id 是否真的落到了每一张核心表。

不调 LLM、不需要 API key、不需要重依赖，只用标准库跑真实的 LedgerStore 代码。

用法：
    python probe_write_path.py
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "probe_write.db"


def load(name, relpath):
    """绕过包导入，避免拉起 hmlr/__init__.py 的重依赖链。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    if DB.exists():
        DB.unlink()

    schema = load("schema", "hmlr/memory/persistence/schema.py")

    # ledger_store 里 `from .schema import DEFAULT_SESSION_ID` 需要包上下文，
    # 这里把它伪装成同名模块即可满足。
    sys.modules["hmlr_probe_pkg"] = type(sys)("hmlr_probe_pkg")
    src = (ROOT / "hmlr/memory/persistence/ledger_store.py").read_text(encoding="utf-8")
    src = src.replace("from .schema import DEFAULT_SESSION_ID",
                      "from schema import DEFAULT_SESSION_ID")
    ns = {"__name__": "ledger_store"}
    exec(compile(src, "ledger_store.py", "exec"), ns)
    LedgerStore = ns["LedgerStore"]

    conn = sqlite3.connect(DB)
    schema.initialize_database(conn)

    # --- 两个会话各写一份数据 -------------------------------------------
    blk_a = LedgerStore.create_new_bridge_block(
        conn, day_id="2026-08-18", topic_label="电商后台重构",
        keywords=["Go", "订单表"], session_id="sess-A",
    )
    blk_b = LedgerStore.create_new_bridge_block(
        conn, day_id="2026-08-18", topic_label="天气爬虫",
        keywords=["爬虫"], session_id="sess-B",
    )

    LedgerStore.append_turn_to_block(conn, blk_a, {
        "turn_id": "turn_a1", "user_message": "订单表分了 9427 张",
        "ai_response": "记下了",
    })
    LedgerStore.append_turn_to_block(conn, blk_b, {
        "turn_id": "turn_b1", "user_message": "帮我抓天气",
        "ai_response": "好的",
    })

    LedgerStore.save_block_metadata(conn, blk_a, ["电商"], [])
    LedgerStore.save_to_gardened_memory(
        conn, [{"chunk_id": "c_a1", "turn_id": "turn_a1",
                "text_verbatim": "订单表分了 9427 张"}], blk_a, ["电商"],
    )

    # fact 先落库（此时还不知道属于哪个 block），再由 link_facts_to_block 回填
    conn.execute(
        "INSERT INTO fact_store (key, value, source_chunk_id, created_at) "
        "VALUES ('table_count','9427','chunk_a1_x','2026-08-18')"
    )
    conn.commit()
    LedgerStore.link_facts_to_block(conn, "turn_a1_x", blk_a)

    # --- 检查 -----------------------------------------------------------
    cur = conn.cursor()
    checks = [
        ("daily_ledger",    "SELECT session_id FROM daily_ledger WHERE block_id=?", blk_a, "sess-A"),
        ("daily_ledger",    "SELECT session_id FROM daily_ledger WHERE block_id=?", blk_b, "sess-B"),
        ("ledger_turns",    "SELECT session_id FROM ledger_turns WHERE turn_id=?", "turn_a1", "sess-A"),
        ("ledger_turns",    "SELECT session_id FROM ledger_turns WHERE turn_id=?", "turn_b1", "sess-B"),
        ("block_metadata",  "SELECT session_id FROM block_metadata WHERE block_id=?", blk_a, "sess-A"),
        ("gardened_memory", "SELECT session_id FROM gardened_memory WHERE chunk_id=?", "c_a1", "sess-A"),
        ("fact_store",      "SELECT session_id FROM fact_store WHERE key=?", "table_count", "sess-A"),
    ]

    print("=" * 60)
    print("写入链路检查")
    print("=" * 60)
    failed = 0
    for table, sql, arg, expected in checks:
        row = cur.execute(sql, (arg,)).fetchone()
        got = row[0] if row else "<no row>"
        if got == expected:
            print(f"  [PASS] {table:16s} -> {got}")
        else:
            print(f"  [FAIL] {table:16s} -> {got}  (expected {expected})")
            failed += 1

    print()
    print("=" * 60)
    print("隔离效果（模拟检索时按 session 过滤）")
    print("=" * 60)
    for s in ("sess-A", "sess-B"):
        rows = cur.execute(
            "SELECT block_id FROM daily_ledger WHERE session_id=?", (s,)
        ).fetchall()
        print(f"  {s} 可见的 block: {[r[0][:14] for r in rows]}")

    a = set(r[0] for r in cur.execute("SELECT block_id FROM daily_ledger WHERE session_id='sess-A'"))
    b = set(r[0] for r in cur.execute("SELECT block_id FROM daily_ledger WHERE session_id='sess-B'"))
    print()
    if a & b:
        print("  [FAIL] 两个会话看到了相同的 block")
        failed += 1
    else:
        print("  [PASS] 两个会话完全不重叠")

    conn.close()
    DB.unlink()

    print()
    print("=" * 60)
    print("全部通过" if failed == 0 else f"{failed} 项失败")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
