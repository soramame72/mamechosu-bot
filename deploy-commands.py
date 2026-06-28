"""
deploy-commands.py
スラッシュコマンドをDiscordに強制登録するスクリプト
コマンドが反映されない・ズレた時だけ使う。

実行: python3.10 deploy-commands.py
"""

import asyncio
import os
import sys

# DEPLOY_MODE を立ててから index.py をロード
os.environ["DEPLOY_MODE"] = "1"

# index.py の bot オブジェクトをそのまま借りる
import index as _idx  # noqa: E402
bot = _idx.bot

def load_env(path="env.txt"):
    env = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

env      = load_env()
TOKEN    = env["TOKEN"]
GUILD_ID = env.get("GUILD_ID")

@bot.event
async def on_ready():
    print(f"ログイン: {bot.user}")
    try:
        if GUILD_ID:
            guild_obj = __import__("discord").Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
            print(f"{len(synced)} コマンドをサーバー ({GUILD_ID}) に登録しました（即時反映）")
        else:
            synced = await bot.tree.sync()
            print(f"{len(synced)} コマンドをグローバルに登録しました（反映まで最大1時間）")
    except Exception as e:
        print(f"同期失敗: {e}")
    finally:
        await bot.close()

bot.run(TOKEN)
