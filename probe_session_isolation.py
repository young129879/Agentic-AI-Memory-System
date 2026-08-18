"""
HMLR 会话隔离可行性探针

目的：验证 session_id 是否真正隔离了记忆。

判定：
  · 会话 B 若能说出只在会话 A 出现过的信息 -> 未隔离
  · 数据库中 Bridge Block 若无 session 维度 -> 未隔离

用法：
  1. 确保 .env 已配好 API key
  2. python probe_session_isolation.py
  3. 只想看数据库结构（不花钱、不调 LLM）：
     python probe_session_isolation.py --schema-only
"""

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "probe_session.db"

# 只在会话 A 出现的独特信息，用于检测泄漏
SECRET_TOKENS = ["Tartarus", "9427", "分库分表"]

SESSION_A = "probe-session-A"
SESSION_B = "probe-session-B"


def hr(title=""):
    print("\n" + "=" * 66)
    if title:
        print(title)
        print("=" * 66)


def ok(msg):
    print(f"  [PASS] {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")


def info(msg):
    print(f"  [INFO] {msg}")


# ---------------------------------------------------------------- 引擎调用

async def send(client, message, session_id):
    """
    调用 HMLR 并返回回复文本。

    HMLRClient.chat() 存在已知 bug（读取了不存在的 response.content），
    因此这里优先尝试门面，失败则降级直连 engine。
    """
    try:
        result = await client.chat(message, session_id=session_id)
        return result.get("content", ""), "client.chat"
    except AttributeError:
        resp = await client.engine.process_user_message(
            message, session_id=session_id
        )
        return getattr(resp, "response_text", str(resp)), "engine(fallback)"


async def run_conversation():
    from hmlr import HMLRClient

    if DB_PATH.exists():
        DB_PATH.unlink()
        info(f"已清除旧数据库: {DB_PATH.name}")

    client = HMLRClient(db_path=str(DB_PATH))

    hr("阶段 1: 会话 A 写入独特信息")
    for msg in [
        "我在做一个电商后台项目，代号 Tartarus，用 Go 语言写的。",
        "订单表做了分库分表，按 user_id 取模，一共 9427 张表。",
    ]:
        reply, via = await send(client, msg, SESSION_A)
        print(f"  A> {msg}")
        print(f"  A< {reply[:110]}")
        info(f"调用路径: {via}")

    hr("阶段 2: 会话 B 聊完全无关的话题")
    reply, _ = await send(client, "帮我写一个抓取天气数据的爬虫。", SESSION_B)
    print(f"  B> 帮我写一个抓取天气数据的爬虫。")
    print(f"  B< {reply[:110]}")

    hr("阶段 3: 关键测试 — 会话 B 是否知道会话 A 的秘密")
    probe = "我之前跟你说过的那个项目代号是什么？订单表分了多少张？"
    reply, _ = await send(client, probe, SESSION_B)
    print(f"  B> {probe}")
    print(f"  B< {reply}")

    leaked = [t for t in SECRET_TOKENS if t.lower() in reply.lower()]

    hr("行为层判定")
    if leaked:
        bad(f"记忆泄漏！会话 B 说出了只属于 A 的信息: {leaked}")
        info("=> session_id 未生效，需要做会话隔离改造")
        return False
    else:
        ok("会话 B 未泄漏 A 的信息")
        info("=> 可能已隔离，请结合下方数据库结构判定")
        return True


# ---------------------------------------------------------------- 数据库检查

# 这些表是系统真正用来构建回答的主力数据
CRITICAL_TABLES = [
    "daily_ledger",      # Bridge Block 本体
    "ledger_turns",      # Block 内的轮次
    "fact_store",        # 事实
    "dossiers",          # 档案
    "dossier_facts",
    "embeddings",        # 向量
    "gardened_memory",
]


def inspect_schema():
    if not DB_PATH.exists():
        bad(f"数据库不存在: {DB_PATH}")
        info("请先不加 --schema-only 跑一次，或指定已有的 db")
        return None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    tables = [
        r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]

    hr("数据库结构: 哪些表带 session_id")
    with_session, without_session = [], []

    for t in tables:
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})").fetchall()]
        has = "session_id" in cols
        (with_session if has else without_session).append(t)
        mark = "有" if has else "无"
        star = " *" if t in CRITICAL_TABLES else ""
        print(f"  [{mark}] {t}{star}")

    print("\n  (* = 系统构建回答时真正依赖的核心表)")

    hr("核心表隔离情况")
    missing = [t for t in CRITICAL_TABLES if t in tables and t not in with_session]

    if not missing:
        ok("全部核心表都有 session_id")
    else:
        bad(f"{len(missing)} 张核心表缺少 session_id:")
        for t in missing:
            print(f"         - {t}")

    # 看看数据实际落在哪
    hr("数据落点抽样")
    for t in ["metadata_staging", "daily_ledger", "ledger_turns", "dossiers"]:
        if t not in tables:
            continue
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = [r[1] for r in cur.execute(f"PRAGMA table_info({t})").fetchall()]
        if "session_id" in cols and n:
            rows = cur.execute(
                f"SELECT session_id, COUNT(*) FROM {t} GROUP BY session_id"
            ).fetchall()
            detail = ", ".join(f"{s}={c}" for s, c in rows)
            print(f"  {t}: {n} 行  ->  {detail}")
        else:
            print(f"  {t}: {n} 行  ->  无 session 维度，全部混在一起")

    conn.close()
    return len(missing) == 0


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--schema-only",
        action="store_true",
        help="只查数据库结构，不调 LLM（不花钱）",
    )
    args = ap.parse_args()

    behavior_ok = None

    if not args.schema_only:
        try:
            behavior_ok = asyncio.run(run_conversation())
        except Exception as e:
            hr("对话测试失败")
            bad(f"{type(e).__name__}: {e}")
            info("常见原因: .env 未配置 API key / 依赖未装 / 首次运行在下载嵌入模型")
            import traceback
            traceback.print_exc()

    schema_ok = inspect_schema()

    hr("最终结论")
    if behavior_ok is False or schema_ok is False:
        bad("会话隔离【未生效】")
        print("""
  改造方案需要包含"阶段 0: 会话隔离"，预计改动 35~40 处：
    · 核心表加 session_id 列 + 数据迁移
    · 写入函数透传 session_id
    · 读取查询加 WHERE session_id = ?
    · 检索入口 govern() / retrieve_context() 加参数

  注意：Dossier 表【不应该】加 session 隔离，
        档案的价值就在于跨会话聚合。""")
    elif behavior_ok and schema_ok:
        ok("会话隔离【已生效】，阶段 0 可以跳过")
    else:
        info("结论不完整，请补跑完整测试")


if __name__ == "__main__":
    main()
