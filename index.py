"""
index.py - Discord Bot メインスクリプト
Python 3.10 + discord.py 2.3.2
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import os
import re
import random
import string
import time
import datetime
import psutil
import aiohttp
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

# ──────────────────────────────────────────────
# 設定読み込み
# ──────────────────────────────────────────────
def load_env(path="env.txt"):
    env = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

env            = load_env()
TOKEN          = env["TOKEN"]
OBAMA_GUILD_ID = int(env.get("OBAMA_GUILD_ID", "1385475575023538236"))
GROQ_API_KEY   = env.get("GROQ_API_KEY", "")
GITHUB_TOKEN   = env.get("GITHUB_TOKEN", "")
AICHAT_API_KEY = env.get("AICHAT_API_KEY", "")

# ──────────────────────────────────────────────
# データ管理 (db/ フォルダ分散JSON)
# ──────────────────────────────────────────────
DB_DIR      = "db"
DATA_FILE   = "date.txt"       # 旧ファイル（マイグレーション用に参照のみ）
DATA_BACKUP = "date.bak.txt"   # 旧バックアップ（同上）

import threading as _threading
_db_lock_store: dict[str, "_threading.Lock"] = {}
_db_lock_store_lock = _threading.Lock()

def _get_file_lock(path: str) -> "_threading.Lock":
    """ファイルパスごとに専用ロックを返す"""
    with _db_lock_store_lock:
        if path not in _db_lock_store:
            _db_lock_store[path] = _threading.Lock()
        return _db_lock_store[path]

def _db_path(feature: str, name: str) -> str:
    """db/{feature}/{name}.json のパスを返し、フォルダも作成する"""
    folder = os.path.join(DB_DIR, feature)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{name}.json")

def db_read(feature: str, guild_id: int | None = None, *, shared: str | None = None) -> dict | list:
    """
    機能フォルダからJSONを読み込む。
    guild_id を指定 → db/{feature}/{guild_id}.json
    shared を指定  → db/{feature}/{shared}.json  (グローバル用)
    """
    name = shared if shared else str(guild_id)
    path = _db_path(feature, name)
    lock = _get_file_lock(path)
    with lock:
        if not os.path.exists(path):
            return [] if shared else {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return [] if shared else {}

def db_write(feature: str, data, guild_id: int | None = None, *, shared: str | None = None):
    """
    機能フォルダへJSONを原子的に書き込む (.tmp → os.replace)。
    guild_id を指定 → db/{feature}/{guild_id}.json
    shared を指定  → db/{feature}/{shared}.json
    """
    name = shared if shared else str(guild_id)
    path = _db_path(feature, name)
    lock = _get_file_lock(path)
    tmp  = path + ".tmp"
    with lock:
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            pass


def db_log(action: str, detail: str = "", level: str = "INFO"):
    """db/bot.log にログを記録する。最大1MBで古い行をトリムして容量を抑制。"""
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_DIR, "bot.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    now_str = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))
    ).strftime("%Y-%m-%d %H:%M:%S")
    short_detail = (detail[:300] + "...") if len(detail) > 300 else detail
    line = f"[{now_str}] [{level}] {action}" + (f" | {short_detail}" if short_detail else "") + "\n"
    lock = _get_file_lock(log_path)
    with lock:
        try:
            if os.path.exists(log_path) and os.path.getsize(log_path) > 1_000_000:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines_buf = f.readlines()
                with open(log_path, "w", encoding="utf-8") as f:
                    f.writelines(lines_buf[int(len(lines_buf) * 0.4):])
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass



# ── レートリミッタ (コマンド/ボタン スパム防止) ──────────
import time as _time
_rate_store: dict[str, float] = {}

def _check_rate(key: str, cooldown_sec: float = 3.0) -> bool:
    """True=実行OK, False=クールダウン中"""
    now = _time.monotonic()
    last = _rate_store.get(key, 0.0)
    if now - last < cooldown_sec:
        return False
    _rate_store[key] = now
    return True

def _rate_key(interaction: discord.Interaction, prefix: str = "") -> str:
    return f"{prefix}:{interaction.user.id}:{interaction.guild_id}"

# ── パスワード試行回数制限 ────────────────────────────────
_pw_attempts: dict[str, list[float]] = {}   # key -> [timestamp, ...]

def _check_password_attempt(user_id: int, role_id: int) -> bool:
    """True=試行OK, False=ロック中 (3回失敗で60秒ロック)"""
    key = f"{user_id}:{role_id}"
    now = _time.monotonic()
    attempts = [t for t in _pw_attempts.get(key, []) if now - t < 60]
    if len(attempts) >= 3:
        return False
    _pw_attempts.setdefault(key, []).append(now)
    _pw_attempts[key] = [t for t in _pw_attempts[key] if now - t < 60]
    return True

def _clear_password_attempt(user_id: int, role_id: int):
    _pw_attempts.pop(f"{user_id}:{role_id}", None)

def gen_code(length=8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ──────────────────────────────────────────────
# Bot 初期化
# ──────────────────────────────────────────────
intents               = discord.Intents.default()
intents.guilds        = True
intents.members       = True
intents.message_content = True
intents.voice_states  = True   # VC接続に必須
intents.messages      = True
intents.reactions     = True
bot        = commands.Bot(command_prefix="!", intents=intents, help_command=None)
START_TIME = time.time()


async def safe_defer(interaction: discord.Interaction, ephemeral=False):
    try:
        await interaction.response.defer(ephemeral=ephemeral)
    except Exception:
        pass


# ──────────────────────────────────────────────
# GROQ ステータス更新 (1分ごと、100/1でえっち喘ぎ声)
# ──────────────────────────────────────────────
# えっちステータス（100分の1の確率で表示）
# update_status は on_ready 内で直接設定するため loop 不要
# (tasks.loop が残っているとインポートエラーになるので空関数で保持)
@tasks.loop(hours=9999)
async def update_status():
    pass


# ──────────────────────────────────────────────
# サーバー移除100日後のデータ消去
# ──────────────────────────────────────────────
@tasks.loop(hours=24)
async def cleanup_removed_guilds():
    """100日以上前に退出したサーバーのデータを全削除する"""
    removed = db_read("removed_guilds", shared="index")
    if not isinstance(removed, dict):
        return
    threshold = 100 * 24 * 3600
    now = time.time()
    to_delete = [gid for gid, ts in removed.items() if now - ts >= threshold]
    if not to_delete:
        return
    for gid in to_delete:
        try:
            db_dir_abs = os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_DIR)
            for feature_dir in os.listdir(db_dir_abs):
                feature_path = os.path.join(db_dir_abs, feature_dir)
                if os.path.isdir(feature_path):
                    guild_file = os.path.join(feature_path, f"{gid}.json")
                    if os.path.exists(guild_file):
                        os.remove(guild_file)
                        db_log("cleanup_guild_data", f"guild_id={gid} feature={feature_dir}")
        except Exception as e:
            db_log("cleanup_guild_data_error", f"guild_id={gid} | {e}", level="ERROR")
        removed.pop(gid, None)
    db_write("removed_guilds", removed, shared="index")
    db_log("cleanup_removed_guilds", f"deleted={to_delete}")

# ──────────────────────────────────────────────
# ランキング用キャッシュと保存タスク（VCのみ）
# ──────────────────────────────────────────────
_vc_ranking_cache: dict[int, dict] = {}
_vc_join_times: dict[int, dict[int, float]] = {}

def _init_vc_ranking(guild_id: int):
    if guild_id not in _vc_ranking_cache:
        data = db_read("vcranking", guild_id=guild_id)
        if not isinstance(data, dict):
            data = {"vc_time": {}}
        _vc_ranking_cache[guild_id] = data

@tasks.loop(minutes=5)
async def task_save_vc_rankings():
    for guild_id, data in _vc_ranking_cache.items():
        if data:
            db_write("vcranking", data, guild_id=guild_id)

# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# on_ready
# ──────────────────────────────────────────────
JST = datetime.timezone(datetime.timedelta(hours=9))

@tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=JST))
async def akeome_loop():
    for guild in bot.guilds:
        data = db_read("akeome", guild_id=guild.id)
        if not data: continue
        target_channels = set()
        if data.get("server"):
            for ch in guild.text_channels:
                target_channels.add(ch)
        else:
            for cid in data.get("channels", []):
                ch = guild.get_channel(cid)
                if ch: target_channels.add(ch)
        for ch in target_channels:
            try: await ch.send("あけおめ")
            except: pass

@bot.tree.command(name="akeomeset", description="毎日0時(JST)にあけおめメッセージを送信するチャンネルを設定します")
@app_commands.describe(channel="送信するチャンネル")
async def cmd_akeomeset(interaction: discord.Interaction, channel: discord.TextChannel):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    data = db_read("akeome", guild_id="global")
    if not isinstance(data, dict):
        data = {}
    data[str(interaction.guild_id)] = channel.id
    db_write("akeome", data, guild_id="global")
    await interaction.followup.send(f"毎日あけおめメッセージを {channel.mention} に送信するように設定しました。", ephemeral=True)

@bot.event
async def on_ready():
    if getattr(bot, "_did_initial_setup", False):
        # 再接続時は状態再設定のみ行い、起動時ログや再同期は行わない
        try:
            await bot.change_presence(activity=discord.CustomActivity(name="ver1.4"))
        except Exception:
            pass
        return
    bot._did_initial_setup = True

    if not akeome_loop.is_running():
        akeome_loop.start()
    print(f"ログイン: {bot.user} (ID: {bot.user.id})")
    try:
        h_cmd = bot.tree.get_command("h")
        if h_cmd:
            h_cmd.nsfw = True
        synced = await bot.tree.sync()
        print(f"{len(synced)} コマンド同期完了")
    except Exception as e:
        print(f"コマンド同期失敗: {e}")
        
    try:
        task_save_active_chats.start()
    except: pass
    try:
        cleanup_removed_guilds.start()
    except: pass
    try:
        task_save_vc_rankings.start()
    except: pass
    try:
        secret_nick_revert_loop.start()
    except: pass

    try:
        bot.add_view(GlobalChatTosView())
    except Exception as e:
        print(f"Failed to add GlobalChatTosView: {e}")

    db_log("bot_start", f"user={bot.user} id={bot.user.id}")

    # AIチャットの永続化データの読み込み
    aichat_data = db_read("aichat", shared="index")
    if isinstance(aichat_data, dict):
        bot._active_chats = getattr(bot, "_active_chats", {})
        count = 0
        for cid_str, s_data in aichat_data.items():
            try: cid = int(cid_str)
            except: continue
            scn = s_data.get("scenario_name", "kouma")
            chars = SCENARIOS.get(scn)
            if not chars: continue
            bot._active_chats[cid] = {
                "chars": chars,
                "scenario_name": scn,
                "topic": s_data.get("topic", "自由な雑談"),
                "history": s_data.get("history", []),
                "task": asyncio.create_task(_chat_loop(cid))
            }
            count += 1
        if count > 0:
            print(f"AIチャット {count} 件を自動復旧しました")

    # ステータスを ver1.4 固定で設定
    await bot.change_presence(
        activity=discord.CustomActivity(name="ver1.4"))

    # Bot起動時にすでにVCにいるユーザーをカウント開始
    now_ts = time.time()
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not member.bot:
                    _vc_join_times.setdefault(guild.id, {})
                    if member.id not in _vc_join_times[guild.id]:
                        _vc_join_times[guild.id][member.id] = now_ts

@bot.event
async def on_error(event_method, *args, **kwargs):
    """未処理の例外はコンソールへ出力せず、ログファイルにのみ記録する。"""
    import traceback
    err_str = traceback.format_exc()
    db_log("unhandled_error", f"event={event_method} | {err_str[:1500]}", level="ERROR")

@bot.event
async def on_guild_remove(guild: discord.Guild):
    removed = db_read("removed_guilds", shared="index")
    if not isinstance(removed, dict):
        removed = {}
    removed[str(guild.id)] = time.time()
    db_write("removed_guilds", removed, shared="index")
    db_log("on_guild_remove", f"guild_id={guild.id} name={guild.name}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    err_str = str(error)
    cmd_name = interaction.command.name if interaction.command else "unknown"
    db_log("cmd_error", f"cmd=/{cmd_name} | {err_str}", level="ERROR")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(f"@silent [エラー] コマンド実行中にエラーが発生しました:\n```\n{err_str[:1900]}\n```", ephemeral=True)
        else:
            await interaction.response.send_message(f"@silent [エラー] コマンド実行中にエラーが発生しました:\n```\n{err_str[:1900]}\n```", ephemeral=True)
    except Exception as e:
        pass


# ──────────────────────────────────────────────
# 2. コマンド一覧 / ヘルプ
# ──────────────────────────────────────────────
@bot.tree.command(name="commands", description="コマンド一覧を表示します")
async def cmd_list(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    cmds = [c for c in bot.tree.get_commands() if hasattr(c, "description") and c.description]
    desc = "\n".join(f"/{c.name} — {c.description}" for c in cmds)
    embed = discord.Embed(title="コマンド一覧", description=desc, color=0x5865F2)
    await interaction.followup.send(embed=embed)

HELP_TEXT = {
    "commands": (
        "**全コマンドを一覧表示します。**\n"
        "使い方: `/commands`\n"
        "各コマンドの名前と概要が確認できます。詳細は `/help [コマンド名]` をご利用ください。"
    ),
    "help": (
        "**各コマンドの詳しい使い方・仕様を表示します。**\n"
        "使い方: `/help` または `/help [コマンド名]`\n"
        "- 省略すると全コマンドの概要一覧を表示\n"
        "- コマンド名を指定するとその詳細ヘルプを表示\n"
        "例: `/help welcome`"
    ),
    "cp": (
        "**コントロールパネル（GUI設定画面）を開きます。**\n"
        "使い方: `/cp`\n"
        "全6ページ構成で以下を管理できます:\n"
        "- 歓迎/送別メッセージ設定\n"
        "- 禁止ワード管理\n"
        "- 認証パネル作成\n"
        "- 自動返信設定\n"
        "- グローバルチャット参加/退出\n"
        "- ロールパネル作成\n"
        "必要権限: チャンネル管理権限"
    ),
    "rolepanel": (
        "**ボタン型ロールパネルを作成します。**\n"
        "使い方: `/rolepanel` → モーダル入力\n"
        "**入力項目:**\n"
        "- ロールID（複数可・カンマ区切り）\n"
        "- パネルタイトル\n"
        "- パスワード（省略可）\n"
        "**仕様:**\n"
        "- ボタン押下でロール付与、再度押すと解除\n"
        "- パスワード設定時は押下後にモーダル入力\n"
        "- 3回連続でパスワードを間違えると60秒ロック\n"
        "- Bot再起動後もボタンは正常動作（永続View）"
    ),
    "welcome": (
        "**メンバー参加時の歓迎メッセージを設定します。**\n"
        "使い方: `/welcome channel:[チャンネル] message:[メッセージ]`\n"
        "**プレースホルダー:**\n"
        "- `{user}` → 参加者へのメンション\n"
        "- `{members}` → 現在のサーバー人数\n"
        "例: `{user} さんようこそ！現在 {members} 人います。`\n"
        "必要権限: サーバー管理権限"
    ),
    "goodbye": (
        "**メンバー退出時の送別メッセージを設定します。**\n"
        "使い方: `/goodbye channel:[チャンネル] message:[メッセージ]`\n"
        "**プレースホルダー:**\n"
        "- `{user}` → 退出者の名前\n"
        "- `{members}` → 退出後のサーバー人数\n"
        "必要権限: サーバー管理権限"
    ),
    "wordblock": (
        "**禁止ワードを登録・管理します。**\n"
        "使い方: `/wordblock action:[add/remove/list] word:[ワード]`\n"
        "**仕様:**\n"
        "- 登録したワードを含むメッセージは自動削除\n"
        "- 絵文字・記号も登録可能\n"
        "- 部分一致で検出（例:「死」登録→「死ぬ」も対象）\n"
        "必要権限: チャンネル管理権限"
    ),
    "verify": (
        "**荒らし対策の認証パネルを設置します。**\n"
        "使い方: `/verify level:[1-10] role:[ロールID] channel:[チャンネル]`\n"
        "**難易度レベル:**\n"
        "- Lv1: ボタンを押すだけ\n"
        "- Lv2: 「同意する」と入力\n"
        "- Lv3: 一桁の計算\n"
        "- Lv4: 二桁の計算\n"
        "- Lv5〜10: 4〜16文字のランダムコード入力\n"
        "認証成功で指定ロールを自動付与\n"
        "必要権限: ロール管理権限"
    ),
    "autoreply": (
        "**特定ワードへの自動返信を設定します。**\n"
        "使い方: `/autoreply action:[add/remove/list] trigger:[ワード]`\n"
        "**仕様:**\n"
        "- トリガーワードを含むメッセージにBotが自動返信\n"
        "- 返信テキストと任意のリアクション絵文字を設定可能\n"
        "- トリガーは部分一致\n"
        "必要権限: チャンネル管理権限"
    ),
    "reaction": (
        "**指定メッセージにobamaリアクションを付与します。**\n"
        "使い方: `/reaction message_id:[メッセージID]`\n"
        "**仕様:**\n"
        "- obama絵文字をランダムに最大25個付与\n"
        "- メッセージIDはメッセージを右クリック→「IDをコピー」で取得\n"
        "- Botが対象のobamaサーバーに在籍している必要があります"
    ),
    "haiku": (
        "**五・七・五（俳句/川柳）自動検出のON/OFFを設定します。**\n"
        "使い方: `/haiku scope:[channel/server] state:[ON/OFF] channel:[チャンネル]`\n"
        "**仕様:**\n"
        "- メッセージが五・七・五のリズムを持つ場合、和紙風画像を生成して送信\n"
        "- `scope:channel` で特定チャンネルのみ有効化\n"
        "- `scope:server` でサーバー全体で有効化\n"
        "必要権限: チャンネル管理権限"
    ),
    "resource": (
        "**Botのシステムリソース状況を表示します。**\n"
        "使い方: `/resource`\n"
        "**表示内容:**\n"
        "- CPU使用率 / メモリ使用率（使用量/総量）\n"
        "- ストレージ使用率\n"
        "- Groq API残りリクエスト数・トークン数\n"
        "- Bot稼働サーバー数"
    ),
    "save": (
        "**サーバー構成をバックアップします。**\n"
        "使い方: `/save`\n"
        "**バックアップ対象:**\n"
        "- 役職（名前・色・権限）\n"
        "- テキスト/ボイスチャンネル（名前・カテゴリ）\n"
        "- チャンネルの権限設定\n"
        "**仕様:**\n"
        "- 実行後に「復元コード」を発行\n"
        "- `/restore` でそのコードを使い別サーバーにも展開可能\n"
        "必要権限: サーバー管理権限"
    ),
    "restore": (
        "**バックアップからサーバー構成を復元します。**\n"
        "使い方: `/restore code:[復元コード]`\n"
        "**仕様:**\n"
        "- `/save` で発行されたコードで役職・チャンネル構成を再作成\n"
        "- 既存のロール・チャンネルは削除されず追加の形で展開\n"
        "- 別サーバーのコードも利用可能（サーバークローン用途）\n"
        "必要権限: サーバー管理権限"
    ),
    "lewd": (
        "**えっち検出機能のON/OFFを設定します。**\n"
        "使い方: `/lewd scope:[channel/server] state:[ON/OFF] channel:[チャンネル]`\n"
        "**仕様:**\n"
        "- 特定のえっちなワードを含むメッセージを検出し「ｴｯﾁﾅﾌﾗﾝﾁｬﾝ」が反応\n"
        "- `scope:channel` で特定チャンネルのみ有効化\n"
        "- `scope:server` でサーバー全体で有効化\n"
        "必要権限: チャンネル管理権限"
    ),
    "atsumori": (
        "**熱盛検知機能のON/OFFを設定します。**\n"
        "使い方: `/atsumori scope:[channel/server] state:[ON/OFF] channel:[チャンネル]`\n"
        "**仕様:**\n"
        "- 検知ワード: 「熱盛」「あつもり」「アツモリ」「ATSUMORI」「atsumori」\n"
        "- 検知するとatsumori.png（熱盛スタンプ）をリプライ送信\n"
        "- 0.5%の確率でランダム誤検知あり（謝罪メッセージ付き）\n"
        "- `scope:channel` / `scope:server` で有効範囲を選択\n"
        "必要権限: チャンネル管理権限"
    ),
    "h": (
        "**えっちな画像をランダムで取得・表示します。**\n"
        "使い方: `/h`\n"
        "**仕様:**\n"
        "- NSFWチャンネル限定（チャンネル設定で「年齢制限」ONが必要）\n"
        "- 80%: yande.re からサンプル画像をDLしてファイル送信\n"
        "- 20%: Rickroll GIF が表示される（リックロール）\n"
        "- リックロール3連続でお祝いメッセージが表示（確率約0.8%）\n"
        "- 取得失敗時は最大3回リトライ"
    ),
    "stats": (
        "**サーバーの活動統計を画像で表示します。**\n"
        "使い方: `/stats days:[日数(1-30)]`\n"
        "**表示内容:**\n"
        "- 総メンバー数・Bot数・人間数・オンライン数\n"
        "- チャンネル数・役職数\n"
        "- 指定期間のメッセージ数・過疎度レベル\n"
        "- グラフィカルな統計画像として出力"
    ),
    "globalchat": (
        "**複数サーバー間のリアルタイムチャットを管理します。**\n"
        "使い方: `/globalchat action:[join/leave/list]`\n"
        "**アクション:**\n"
        "- `join` : 実行チャンネルをグローバルチャットに参加\n"
        "- `leave` : 実行チャンネルを退出\n"
        "- `list` : 現在の参加チャンネル一覧を表示\n"
        "**仕様:**\n"
        "- Webhookで他サーバーの発言をリレー\n"
        "- 初回発言時はDMで利用規約への同意が必要\n"
        "- 3秒以内の連投・同内容の重複送信は自動削除\n"
        "- 連続スパムで5分間ブロック\n"
        "必要権限: チャンネル管理権限"
    ),
    "permission": (
        "**Botの現在の権限状態を確認します。**\n"
        "使い方: `/permission`\n"
        "**表示内容:**\n"
        "- [OK] 付与済み権限一覧\n"
        "- [NG] 不足している権限一覧\n"
        "- 付与されているロール一覧\n"
        "- BotのPing（ms）\n"
        "権限不足の場合は一部機能が動作しないことがあります"
    ),
    "quote": (
        "**テキストを名言風の画像カードに変換します。**\n"
        "使い方: `/quote text:[文章] author:[著者名] theme:[テーマ]`\n"
        "**オプション:**\n"
        "- `text` : 表示するテキスト（必須）\n"
        "- `author` : 著者名（省略可）\n"
        "- `theme` : 背景テーマ（省略可）\n"
        "画像ファイルとして出力されます"
    ),
    "purge": (
        "**チャンネルのメッセージを一括削除します。**\n"
        "使い方: `/purge count:[件数(1-100)]`\n"
        "**仕様:**\n"
        "- 直近のメッセージを指定件数だけ削除（最大100件）\n"
        "- 14日以上前のメッセージはDiscord制限で削除不可\n"
        "必要権限: メッセージ管理権限"
    ),
    "grok": (
        "**BotのGitHubリポジトリリンクを表示します。**\n"
        "使い方: `/grok`\n"
        "このBotのベースプロジェクト `grok_dc` のリンクを表示します。"
    ),
    "supiki": (
        "**ｽﾋﾟｷになります。**\n"
        "使い方: `/supiki`\n"
        "隠しコマンドです。「ｽﾋﾟｷ」のアイコンを持つWebhookがランダムなセリフを発言します。"
    ),
    "chat": (
        "**AIキャラクターの自律会話を開始・停止します。**\n"
        "使い方: `/chat action:[start/stop] scenario:[シナリオ] topic:[話題] interval_min:[分] interval_max:[分]`\n"
        "**アクション:**\n"
        "- `action:start` → 会話を開始\n"
        "- `action:stop` → 会話を停止\n"
        "**オプション:**\n"
        "- `scenario` : 参加キャラクターのシナリオ（例: kouma=紅魔館）\n"
        "- `topic` : 話題の指定（省略すると「自由な雑談」）\n"
        "- `interval_min/max` : 発言間隔の最小・最大（分単位、管理者のみ変更可）\n"
        "**仕様:**\n"
        "- Webhookでキャラ固有アイコン・名前で発言\n"
        "- Groq API (openai/gpt-oss-120b) でAI応答を生成\n"
        "- Bot再起動後も自動復旧（永続化）\n"
        "- 一般ユーザーの発言にキャラが反応することがあります"
    ),
    "apikey": (
        "**サーバー独自のGroq APIキーを設定します（管理者専用）。**\n"
        "使い方: `/apikey api_key:[Groq APIキー]`\n"
        "**仕様:**\n"
        "- 設定すると、このサーバーのAI機能（川柳検出・えっち検出・熱盛検知・ローマ字翻訳・meigen・sakubun・AIチャット等）すべてがそのキーを使用します。AIチャット専用ではありません。\n"
        "- 空で実行するとカスタムキーを削除してデフォルトに戻す\n"
        "- Groq APIキーは https://console.groq.com/ で無料発行可能\n"
        "必要権限: サーバー管理権限"
    ),
    "echo": (
        "**入力したメッセージをBotがそのまま発言します。**\n"
        "使い方: `/echo message:[メッセージ]`\n"
        "Botに喋らせたい文章を入力すると、Botが代わりに発言します。\n"
        "[注意] 誰が使ったかはメッセージの最後に表示されます。\n"
        "必要権限: 管理者権限"
    ),
    "ranking": (
        "**サーバー内の活動ランキング（TOP 10）を表示します。**\n"
        "使い方: `/ranking category:[部門]`\n"
        "**部門一覧:**\n"
        "- `メッセージ送信数` → 送信回数\n"
        "- `添付ファイル数` → 画像・動画などの送信数\n"
        "- `合計文字数` → 送信メッセージの文字数合計\n"
        "- `リアクション獲得数` → 他のメンバーからもらったリアクション總数\n"
        "- `URL送信数` → URL/リンクを送信した数\n"
        "- `メンション送信数` → 誰かをメンションした回数\n"
        "- `VC滞在時間` → Bot導入からの累計（即座表示）\n"
        "※メッセージ系部門は履歴スキャンのため表示まで数秒かかることがあります。"
    ),
    "spam": (
        "**指定したメッセージを指定回数、一定間隔で連投します。**\n"
        "使い方: `/spam message:[メッセージ] count:[回数(最大50)] interval:[間隔(0.5秒以上)]`\n"
        "必要権限: メッセージ管理権限"
    ),
    "reaction": (
        "**指定メッセージにObama絵文字25個をランダムでつけます。**\n"
        "使い方: `/reaction message_id:[メッセージID]`\n"
        "必要権限: メッセージ管理権限"
    ),
    "cp": (
        "**サーバーの各種設定をボタンで操作できるパネルを開きます。**\n"
        "使い方: `/cp`\n"
        "必要権限: チャンネル管理権限"
    ),
    "cp_help": (
        "**CP（コントロールパネル）のヘルプを表示します。**\n"
        "使い方: `/cp_help`\n"
        "必要権限: チャンネル管理権限"
    ),
    "rolepanel": (
        "**ボタンでロールを付与/剥奪できるパネルを作成します。**\n"
        "使い方: `/rolepanel roles:[ロールメンション] title:[タイトル] password:[パスワード(任意)]`\n"
        "必要権限: ロール管理権限"
    ),
    "akeomeset": (
        "**毎日0時にあけおめメッセージを自動送信するチャンネルを設定します。**\n"
        "使い方: `/akeomeset channel:[チャンネル]`\n"
        "必要権限: チャンネル管理権限"
    ),
    "impersonate": (
        "**指定したユーザーになりすまして発言します。**\n"
        "使い方: `/impersonate user:[ユーザー] message:[発言内容] attachment:[画像]`\n"
        "※一定確率で正体がバレる煽りメッセージが表示されます。\n"
        "必要権限: メッセージ管理権限"
    ),
    "impersonatechance": (
        "**なりすましがバレる確率を設定します。**\n"
        "使い方: `/impersonatechance percent:[0〜100]`\n"
        "必要権限: チャンネル管理権限"
    ),
    "impersonateset": (
        "**なりすまし機能のON/OFFを設定します。**\n"
        "使い方: `/impersonateset scope:[channel/server] state:[ON/OFF] channel:[対象]`\n"
        "必要権限: チャンネル管理権限"
    ),
    "romaji": (
        "**ローマ字翻訳機能のON/OFFを設定します。**\n"
        "使い方: `/romaji scope:[channel/server] state:[ON/OFF] channel:[対象]`\n"
        "必要権限: チャンネル管理権限"
    ),
    "rolepanel": (
        "**リアクション絵文字でロールを付与/剥奪できるパネルを作成します。**\n"
        "使い方: `/rolepanel roles_and_emojis:[絵文字:ロール...] title:[タイトル] password:[パスワード(任意)]`\n"
        "**入力例:** `🍎:@Member, 🍇:@Gamer`\n"
        "**仕様:**\n"
        "- リアクションを押すと即座にロールが付与（再度押すと剥奪）\n"
        "- 自分のリアクションは即座に消去される\n"
        "- パスワード付きの場合はDMにボタンが届きそこから入力\n"
        "- ロール名の隣に現在の人数が表示され、付与/剥奪のたびに自動更新\n"
        "- パネルに登録していない絵文字でリアクションされた場合は静かに削除される\n"
        "- Botが使用できない絵文字（Botが参加していないサーバーの絵文字等）が含まれる場合は作成後に警告を表示\n"
        "必要権限: ロール管理権限"
    ),
    "rolepaneledit": (
        "**既存のロールパネルを編集します。**\n"
        "使い方: `/rolepaneledit message_id:[メッセージID] roles_and_emojis:[絵文字:ロール...] title:[任意] password:[任意] remove_password:[任意]`\n"
        "**仕様:**\n"
        "- ロール構成を丸ごと新しい内容に置き換える（既存のリアクションは一旦クリアされ、新しい絵文字が付け直される）\n"
        "- `title` / `password` を省略すると現在の設定を維持\n"
        "- パスワードを解除したい場合は `remove_password:True` を指定\n"
        "- Botが使用できない絵文字が含まれる場合は編集後に警告を表示\n"
        "必要権限: ロール管理権限（`/rolepanel` と同じ）"
    ),
    "meigen": (
        "**過去のメッセージからAIが名言/迷言を発掘し、名言カード画像を生成します。**\n"
        "使い方: `/meigen quote_type:[迷言/名言] channel:[チャンネル]`\n"
        "**オプション:**\n"
        "- `quote_type` : 迷言（面白い発言）または名言（良い発言）を選択\n"
        "- `channel` : 検索対象チャンネル（省略すると現在のチャンネル）\n"
        "**仕様:**\n"
        "- 過去1000件のメッセージからランダムに100件サンプリング\n"
        "- Groq AI（openai/gpt-oss-120b）が該当発言を選出\n"
        "- 選出結果は画像カードとメッセージリンク付きで送信\n"
        "- Groq APIが利用不可な場合はランダムフォールバック"
    ),
    "sakubun": (
        "**指定したテーマと文字数でAIが作文を書き、原稿用紙画像として出力します。**\n"
        "使い方: `/sakubun theme:[テーマ] length:[文字数]`\n"
        "**オプション:**\n"
        "- `theme` : 作文のテーマ（必須）\n"
        "- `length` : 第1型200/400/600字から選択\n"
        "**仕様:**\n"
        "- Groq AIで作文を生成\n"
        "- 原稿用紙デザインの画像として出力"
    ),
    "letterreact": (
        "**指定したメッセージに文字の絵文字でリアクションします。**\n"
        "使い方: `/letterreact message_id:[メッセージID] text:[文字]`\n"
        "**対応文字:**\n"
        "- A～Z / a～z → 🆬～🇿 (地域指示絵文字)\n"
        "- 0～9 → 0️⃣～9️⃣ (キーキャップ数字)\n"
        "- `! ? + -` など一部記号\n"
        "**仕様:**\n"
        "- 同じ文字が重複する場合は最初の1件のみリアクション（Discordの仕様）\n"
        "必要権限: メッセージ管理権限"
    ),
    "secret": (
        "**【使用注意！！】このコマンドは何が起こるかわかりません！身内鯖以外での使用は推奨しません。**\n"
        "使い方: `/secret`\n"
        "**仕様:**\n"
        "実行するたびに、以下からランダムで1つだけいたずらが発生します。\n"
        "必要権限: 管理者権限のみ"
    ),
}

@bot.tree.command(name="help", description="各コマンドの使い方を表示します")
@app_commands.describe(command="調べたいコマンド名（省略すると全体）")
async def cmd_help(interaction: discord.Interaction, command: str = None):
    await safe_defer(interaction, ephemeral=True)
    if command and command in HELP_TEXT:
        embed = discord.Embed(title=f"/{command}", description=HELP_TEXT[command], color=0x57F287)
        await interaction.followup.send(embed=embed)
    else:
        embeds = []
        embed = discord.Embed(title="ヘルプ (1)", color=0x57F287)
        for i, (k, v) in enumerate(HELP_TEXT.items()):
            if len(embed.fields) >= 25:
                embeds.append(embed)
                embed = discord.Embed(title=f"ヘルプ ({len(embeds)+1})", color=0x57F287)
            embed.add_field(name=f"/{k}", value=v.split("\n")[0], inline=False)
        embeds.append(embed)
        embeds[-1].set_footer(text="/help [コマンド名] で詳細を表示")
        await interaction.followup.send(embeds=embeds)

@bot.tree.command(name="echo", description="入力したメッセージをBotがそのまま発言します")
@app_commands.describe(message="発言させたいメッセージ")
@app_commands.default_permissions(administrator=True)
async def cmd_echo(interaction: discord.Interaction, message: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("管理者権限が必要です。", ephemeral=True)
        return
    # 普通にレスポンスを返すことで「誰がコマンドを実行したか」がDiscord標準のUIで表示される
    await interaction.response.send_message(message)

import uuid

class SpamView(discord.ui.View):
    def __init__(self, spams_dict, spam_id):
        super().__init__(timeout=None)
        self.spams_dict = spams_dict
        self.spam_id = spam_id

    @discord.ui.button(label="停止", style=discord.ButtonStyle.danger)
    async def stop_spam(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.spams_dict.get(self.spam_id)
        if entry is not None:
            if isinstance(entry, dict):
                entry["running"] = False
            else:
                self.spams_dict[self.spam_id] = False
            await interaction.response.send_message("スパムを停止しました。", ephemeral=True)
        else:
            await interaction.response.send_message("すでに終了しているか、停止済みです。", ephemeral=True)
        super().stop()

@bot.tree.command(name="spam", description="指定したメッセージを指定回数、一定間隔で連投します（実行中に再実行すると停止）")
@app_commands.describe(message="連投するメッセージ", count="連投回数（最大50回）", interval="間隔（秒、0.5秒以上）")
async def cmd_spam(interaction: discord.Interaction, message: str, count: int, interval: float = 1.0):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("このコマンドを実行するには「メッセージの管理」権限が必要です。", ephemeral=True)
        return

    if not hasattr(bot, "_active_spams"):
        bot._active_spams = {}

    # チャンネルにアクティブなスパムがある場合は停止
    ch_id = interaction.channel_id
    for sid, active in list(bot._active_spams.items()):
        if isinstance(active, dict) and active.get("channel") == ch_id and active.get("running"):
            active["running"] = False
            await interaction.followup.send("実行中のスパムを停止しました。", ephemeral=True)
            return
    
    if count < 1 or count > 50:
        await interaction.followup.send("連投回数は 1回〜50回 の間で指定してください。", ephemeral=True)
        return
        
    if interval < 0.5:
        await interaction.followup.send("間隔は 0.5秒 以上に設定してください。（API制限回避のため）", ephemeral=True)
        return
        
    if len(message) > 2000:
        await interaction.followup.send("メッセージは2000文字以内にしてください。", ephemeral=True)
        return
        
    spam_id = str(uuid.uuid4())
    bot._active_spams[spam_id] = {"channel": ch_id, "running": True}
    
    view = SpamView(bot._active_spams, spam_id)
    await interaction.followup.send(f"スパムを開始します（{count}回, {interval}秒間隔）\n再度 /spam を実行するか「停止」ボタンでスパムを停止できます。", view=view, ephemeral=True)
    
    for i in range(count):
        if not bot._active_spams.get(spam_id, {}).get("running", False):
            break
        try:
            await interaction.channel.send(message)
        except Exception:
            break
        if i < count - 1:
            await asyncio.sleep(interval)
            
    bot._active_spams.pop(spam_id, None)

@bot.tree.command(name="ranking", description="サーバー内の活動ランキング（TOP 10）を表示します")
@app_commands.describe(category="ランキングの部門")
@app_commands.choices(category=[
    app_commands.Choice(name="総合（スコア制）",      value="total"),
    app_commands.Choice(name="メッセージ送信数",     value="msg_count"),
    app_commands.Choice(name="添付ファイル数",     value="attachment_count"),
    app_commands.Choice(name="合計文字数",       value="char_count"),
    app_commands.Choice(name="リアクション獲得数",   value="reaction_count"),
    app_commands.Choice(name="URL送信数",        value="url_count"),
    app_commands.Choice(name="メンション送信数",     value="mention_count"),
    app_commands.Choice(name="VC滞在時間",       value="vc_time"),
])
async def cmd_ranking(interaction: discord.Interaction, category: str = "msg_count"):
    await safe_defer(interaction)
    guild = interaction.guild
    medals = ["1位", "2位", "3位", "4位", "5位", "6位", "7位", "8位", "9位", "10位"]

    title_map = {
        "total":           "総合スコア",
        "msg_count":       "メッセージ送信数",
        "attachment_count": "添付ファイル送信数",
        "char_count":      "合計文字数",
        "reaction_count":  "リアクション獲得数",
        "url_count":       "URL送信数",
        "mention_count":   "メンション送信数",
        "vc_time":         "ボイスチャット滞在時間",
    }
    title = title_map.get(category, category)

    if category == "vc_time":
        # VC数はDBから即座に取得
        _init_vc_ranking(guild.id)
        now_t = time.time()
        temp_vc_add = {}
        for uid, join_time in _vc_join_times.get(guild.id, {}).items():
            temp_vc_add[str(uid)] = now_t - join_time

        data = _vc_ranking_cache[guild.id].get("vc_time", {})
        combined = {}
        for uid_str, val in data.items():
            combined[uid_str] = combined.get(uid_str, 0) + val
        for uid_str, val in temp_vc_add.items():
            combined[uid_str] = combined.get(uid_str, 0) + val

        sorted_data = sorted(combined.items(), key=lambda x: x[1], reverse=True)[:10]
        if not sorted_data:
            embed = discord.Embed(title=f"【{title} ランキング】", description="まだデータがありません。", color=0xFFD700)
            await interaction.followup.send(embed=embed)
            return

        desc = ""
        for i, (uid_str, val) in enumerate(sorted_data):
            user = guild.get_member(int(uid_str))
            name = user.display_name if user else f"不明(ID:{uid_str})"
            hours = int(val // 3600)
            mins  = int((val % 3600) // 60)
            time_str = f"{hours}時間{mins}分" if hours > 0 else f"{mins}分"
            desc += f"{medals[i]} **{name}** : {time_str}\n"

        embed = discord.Embed(title=f"【{title} ランキング (TOP 10)】", description=desc, color=0xFFD700)
        embed.set_footer(text="※データはBot導入/再起動以降の累計です")
        await interaction.followup.send(embed=embed)
        return

    # --- メッセージ履歴スキャン系（msg_count / attachment_count / char_count / reaction_count）---
    await interaction.followup.send("過去の履歴から集計しています。しばらくお待ちください...", ephemeral=True)
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    since  = now_dt - datetime.timedelta(days=30)

    msg_counts        = {}
    attachment_counts = {}
    char_counts       = {}
    reaction_counts   = {}
    url_counts        = {}
    mention_counts    = {}

    import re
    url_pattern = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+")

    for ch in guild.text_channels:
        try:
            async for msg in ch.history(after=since, limit=1000):
                if msg.author.bot:
                    continue
                uid = str(msg.author.id)
                msg_counts[uid]        = msg_counts.get(uid, 0)        + 1
                attachment_counts[uid] = attachment_counts.get(uid, 0) + len(msg.attachments)
                char_counts[uid]       = char_counts.get(uid, 0)       + len(msg.content)
                rxn = sum(r.count for r in msg.reactions)
                reaction_counts[uid]   = reaction_counts.get(uid, 0)   + rxn
                url_counts[uid]        = url_counts.get(uid, 0)        + len(url_pattern.findall(msg.content))
                mention_counts[uid]    = mention_counts.get(uid, 0)    + len(msg.mentions)
        except Exception:
            pass

    data_map = {
        "msg_count":        msg_counts,
        "attachment_count": attachment_counts,
        "char_count":       char_counts,
        "reaction_count":   reaction_counts,
        "url_count":        url_counts,
        "mention_count":    mention_counts,
    }
    if category == "total":
        total_counts = {}
        for uid in msg_counts:
            total_counts[uid] = (
                msg_counts.get(uid, 0) * 1 +
                attachment_counts.get(uid, 0) * 3 +
                reaction_counts.get(uid, 0) * 2 +
                url_counts.get(uid, 0) * 1 +
                int(char_counts.get(uid, 0) * 0.01)
            )
        counts = total_counts
    else:
        counts = data_map.get(category, {})
    sorted_data = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]

    if not sorted_data:
        embed = discord.Embed(title=f"【{title} ランキング】", description="まだデータがありません。", color=0xFFD700)
        await interaction.channel.send(embed=embed)
        return

    desc = ""
    for i, (uid_str, val) in enumerate(sorted_data):
        user = guild.get_member(int(uid_str))
        name = user.display_name if user else f"不明(ID:{uid_str})"
        if category == "char_count":
            desc += f"{medals[i]} **{name}** : {val:,} 文字\n"
        elif category == "attachment_count":
            desc += f"{medals[i]} **{name}** : {val} 件\n"
        elif category == "reaction_count":
            desc += f"{medals[i]} **{name}** : {val} 個\n"
        else:
            desc += f"{medals[i]} **{name}** : {val} 回\n"

    embed = discord.Embed(title=f"【{title} ランキング (TOP 10)】", description=desc, color=0xFFD700)
    embed.set_footer(text="※直近30日間（各チャンネル最大1000件まで）の集計結果です")
    await interaction.channel.send(embed=embed)

# ──────────────────────────────────────────────
# 4. コントロールパネル /cp
# ──────────────────────────────────────────────
# ── コントロールパネル用モーダル ──────────────────────────────
# ──────────────────────────────────────────────
# コントロールパネル (CP) - 完全サブクラス実装
# ──────────────────────────────────────────────

# ── モーダル群 ────────────────────────────────
class WelcomeSetModal(discord.ui.Modal, title="歓迎メッセージ設定"):
    ch_id = discord.ui.TextInput(label="チャンネルID", placeholder="チャンネルを右クリック→IDをコピー")
    msg   = discord.ui.TextInput(label="メッセージ ({user}=メンション {members}=人数)",
                                  style=discord.TextStyle.paragraph)
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        try:
            cid = int(self.ch_id.value.strip())
            db_write("welcome", {"channel": cid, "message": self.msg.value}, guild_id=self.gid)
            ch = interaction.guild.get_channel(cid)
            await interaction.response.send_message(
                f"歓迎メッセージを設定しました → {ch.mention if ch else cid}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class GoodbyeSetModal(discord.ui.Modal, title="送別メッセージ設定"):
    ch_id = discord.ui.TextInput(label="チャンネルID", placeholder="チャンネルを右クリック→IDをコピー")
    msg   = discord.ui.TextInput(label="メッセージ ({user}=名前 {members}=人数)",
                                  style=discord.TextStyle.paragraph)
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        try:
            cid = int(self.ch_id.value.strip())
            db_write("goodbye", {"channel": cid, "message": self.msg.value}, guild_id=self.gid)
            ch = interaction.guild.get_channel(cid)
            await interaction.response.send_message(
                f"送別メッセージを設定しました → {ch.mention if ch else cid}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class WordAddModal(discord.ui.Modal, title="禁止ワード追加"):
    word = discord.ui.TextInput(label="追加するワード")
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        gd = db_read("wordblock", guild_id=self.gid)
        bl = gd.get("words", [])
        w  = self.word.value.strip()
        if w and w not in bl: bl.append(w)
        db_write("wordblock", {"words": bl}, guild_id=self.gid)
        await interaction.response.send_message(f"`{w}` を追加しました。", ephemeral=True)

class AutoreplyAddModal(discord.ui.Modal, title="自動返信追加"):
    trigger = discord.ui.TextInput(label="トリガーワード")
    reply   = discord.ui.TextInput(label="返信テキスト", style=discord.TextStyle.paragraph)
    emoji   = discord.ui.TextInput(label="リアクション絵文字（省略可）", required=False)
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        gd = db_read("autoreply", guild_id=self.gid)
        ar = gd.get("replies", {})
        ar[self.trigger.value] = {"text": self.reply.value, "emoji": self.emoji.value or ""}
        db_write("autoreply", {"replies": ar}, guild_id=self.gid)
        await interaction.response.send_message(
            f"自動返信を追加: `{self.trigger.value}`", ephemeral=True)


class VerifySetModal(discord.ui.Modal, title="認証パネル作成"):
    level   = discord.ui.TextInput(label="保護レベル (1〜10)", placeholder="3")
    role_id = discord.ui.TextInput(label="付与するロールID", placeholder="ロールを右クリック→IDをコピー")
    def __init__(self, guild_id, channel_id): super().__init__(); self.gid = guild_id; self.cid = channel_id
    async def on_submit(self, interaction):
        try:
            lv   = int(self.level.value.strip())
            rid  = int(self.role_id.value.strip())
            role = interaction.guild.get_role(rid)
            if not role:
                await interaction.response.send_message("ロールが見つかりません。", ephemeral=True); return
            if not 1 <= lv <= 10:
                await interaction.response.send_message("レベルは1〜10です。", ephemeral=True); return
            ch = interaction.guild.get_channel(self.cid)
            if not ch:
                await interaction.response.send_message("チャンネルが見つかりません。", ephemeral=True); return
            VERIFY_LEVELS = {1:"ボタンを押すだけ",2:"「同意する」と入力",3:"一桁の計算",
                             4:"二桁の計算",5:"4文字コード",6:"6文字コード",7:"8文字コード",
                             8:"10文字コード",9:"12文字コード",10:"16文字コード"}
            embed = discord.Embed(title="認証パネル",
                description=f"レベル {lv} / {VERIFY_LEVELS[lv]}\nボタンを押して認証してください。",
                color=0xEB459E)
            # VerifyView は後で定義されているのでここでは import 的に呼ぶ
            await ch.send(embed=embed, view=VerifyView(lv, rid))
            await interaction.response.send_message(
                f"#{ch.name} に認証パネルを作成しました（レベル{lv}）。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

def _normalize_reaction_emoji(emoji_str: str):
    """カスタム絵文字の <:name:id> / <a:name:id> 形式を add_reaction が受け付ける形に正規化する。
    Unicode絵文字の場合はそのまま返す。"""
    try:
        if emoji_str.startswith("<") and emoji_str.endswith(">"):
            return discord.PartialEmoji.from_str(emoji_str)
    except Exception:
        pass
    return emoji_str

_CUSTOM_EMOJI_TAG = re.compile(r"^(<a?:[^:>]+:\d+>)\s*:\s*(.+)$")

def parse_roles_and_emojis(guild, text: str):
    mapping = {}
    for part in text.split(","):
        part = part.strip()
        if not part: continue

        m_custom = _CUSTOM_EMOJI_TAG.match(part)
        if m_custom:
            emoji_str = m_custom.group(1)
            role_str = m_custom.group(2).strip()
        else:
            if ":" not in part: continue
            emoji_str, role_str = part.split(":", 1)
            emoji_str = emoji_str.strip()
            role_str = role_str.strip()

        m = re.search(r"<@&(\d+)>", role_str)
        if m:
            role_id = int(m.group(1))
        elif role_str.isdigit():
            role_id = int(role_str)
        else:
            # ロール名で解決（先頭の@は除去、大文字小文字は区別しない）
            name = role_str[1:] if role_str.startswith("@") else role_str
            role_obj = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
            if not role_obj: continue
            role_id = role_obj.id
            
        role = guild.get_role(role_id)
        if role:
            mapping[emoji_str] = role.id
    return mapping

def _build_rolepanel_embed(guild, title: str, mapping: dict, password: str | None):
    desc = "リアクションを押すことでロールを取得/解除できます。\n\n"
    for emoji_str, role_id in mapping.items():
        r = guild.get_role(role_id)
        if r:
            desc += f"{emoji_str}：{r.mention}（{len(r.members)}人）\n"
    embed = discord.Embed(title=title, description=desc, color=0x5865F2)
    if password:
        embed.set_footer(text="このパネルはパスワード保護されています (DMに届きます)")
    return embed

async def _add_rolepanel_reactions(msg, mapping: dict) -> list:
    failed = []
    for emoji_str in mapping.keys():
        try:
            await msg.add_reaction(_normalize_reaction_emoji(emoji_str))
        except Exception:
            failed.append(emoji_str)
    return failed

def _rolepanel_failed_warning(failed: list) -> str:
    if not failed:
        return ""
    return ("\n⚠️ 以下の絵文字はBotが使用できないため付与できませんでした（Botが参加していないサーバーの絵文字である可能性があります）: "
            + ", ".join(failed))

class RolePanelModal(discord.ui.Modal, title="ロールパネル作成"):
    roles_input = discord.ui.TextInput(label="絵文字:ロールID をカンマ区切りで入力",
                                        placeholder="🍎:123456789, 🍇:987654321")
    panel_title = discord.ui.TextInput(label="パネルタイトル", default="ロールパネル")
    password    = discord.ui.TextInput(label="パスワード（省略可）", required=False)
    
    def __init__(self, guild_id, channel_id): super().__init__(); self.gid = guild_id; self.cid = channel_id
    
    async def on_submit(self, interaction):
        try:
            guild = interaction.guild
            ch = guild.get_channel(self.cid)
            mapping = parse_roles_and_emojis(guild, self.roles_input.value)
            
            if not mapping:
                await interaction.response.send_message("有効な「絵文字:ロール」のペアが見つかりません。", ephemeral=True); return
                
            pw = self.password.value.strip() or None
            embed = _build_rolepanel_embed(guild, self.panel_title.value, mapping, pw)
            
            msg = await ch.send(embed=embed)
            db_write("reaction_roles", {"guild_id": guild.id, "channel_id": ch.id, "title": self.panel_title.value,
                                         "roles": mapping, "password": pw}, shared=str(msg.id))
            failed = await _add_rolepanel_reactions(msg, mapping)
                
            await interaction.response.send_message("ロールパネルを作成しました。" + _rolepanel_failed_warning(failed), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class RolePanelEditModal(discord.ui.Modal, title="ロールパネル編集"):
    message_id  = discord.ui.TextInput(label="編集するパネルのメッセージID")
    roles_input = discord.ui.TextInput(label="絵文字:ロールID をカンマ区切りで入力",
                                        placeholder="🍎:123456789, 🍇:987654321")
    panel_title = discord.ui.TextInput(label="パネルタイトル（空欄で維持）", required=False)
    password    = discord.ui.TextInput(label="パスワード（空欄で維持、解除は「-」）", required=False)

    def __init__(self, guild_id, channel_id): super().__init__(); self.gid = guild_id; self.cid = channel_id

    async def on_submit(self, interaction):
        try:
            guild = interaction.guild
            try:
                mid = int(self.message_id.value.strip())
            except ValueError:
                await interaction.response.send_message("メッセージIDは数字で指定してください。", ephemeral=True); return

            panel_data = db_read("reaction_roles", shared=str(mid))
            if not panel_data or not isinstance(panel_data, dict) or panel_data.get("guild_id") != guild.id:
                await interaction.response.send_message("指定したメッセージIDのロールパネルが見つかりません。", ephemeral=True); return

            ch_id = panel_data.get("channel_id") or self.cid
            channel = guild.get_channel_or_thread(ch_id)
            if not channel:
                await interaction.response.send_message("パネルのチャンネルが見つかりません。", ephemeral=True); return
            try:
                msg = await channel.fetch_message(mid)
            except Exception:
                await interaction.response.send_message("パネルのメッセージが見つかりません（削除された可能性があります）。", ephemeral=True); return

            mapping = parse_roles_and_emojis(guild, self.roles_input.value)
            if not mapping:
                await interaction.response.send_message("有効な「絵文字:ロール」のペアが見つかりません。", ephemeral=True); return

            new_title = self.panel_title.value.strip() or panel_data.get("title", "ロールパネル")
            pw_input = self.password.value.strip()
            if pw_input == "-":
                new_pw = None
            elif pw_input:
                new_pw = pw_input
            else:
                new_pw = panel_data.get("password")

            embed = _build_rolepanel_embed(guild, new_title, mapping, new_pw)
            await msg.edit(embed=embed)
            await msg.clear_reactions()
            failed = await _add_rolepanel_reactions(msg, mapping)

            db_write("reaction_roles", {"guild_id": guild.id, "channel_id": channel.id, "title": new_title,
                                         "roles": mapping, "password": new_pw}, shared=str(mid))
            await interaction.response.send_message("ロールパネルを編集しました。" + _rolepanel_failed_warning(failed), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class GlobalChatModal(discord.ui.Modal, title="グローバルチャット設定"):
    action = discord.ui.TextInput(label="アクション (join / leave)", placeholder="join")
    def __init__(self, guild_id, channel_id): super().__init__(); self.gid = guild_id; self.cid = channel_id
    async def on_submit(self, interaction):
        act = self.action.value.strip().lower()
        ch  = interaction.guild.get_channel(self.cid)
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True); return
        channels = get_global_channels()
        if act == "join":
            if any(c["channel_id"] == ch.id for c in channels):
                await interaction.response.send_message("すでに参加中です。", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            wh = await get_or_create_webhook(ch)
            if not wh:
                await interaction.followup.send("Webhook作成失敗。「ウェブフックの管理」権限を確認してください。", ephemeral=True); return
            channels.append({"guild_id": interaction.guild_id, "channel_id": ch.id,
                              "guild_name": interaction.guild.name, "channel_name": ch.name, "webhook_url": wh})
            set_global_channels(channels)
            await interaction.followup.send(f"#{ch.name} をグローバルチャットに追加しました。", ephemeral=True)
        elif act == "leave":
            new = [c for c in channels if c["channel_id"] != ch.id]
            set_global_channels(new)
            await interaction.response.send_message(f"#{ch.name} を退出しました。", ephemeral=True)
        else:
            await interaction.response.send_message("join または leave を入力してください。", ephemeral=True)

class PurgeModal(discord.ui.Modal, title="メッセージ一括削除"):
    count = discord.ui.TextInput(label="削除する件数 (1〜100)", placeholder="10")
    def __init__(self, channel): super().__init__(); self.channel = channel
    async def on_submit(self, interaction):
        try:
            n = int(self.count.value.strip())
            if not 1 <= n <= 100:
                await interaction.response.send_message("1〜100で指定してください。", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            deleted = await self.channel.purge(limit=n)
            await interaction.followup.send(f"{len(deleted)} 件削除しました。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

# ── CPボタンのベースクラス（全てサブクラス化） ─────────────
class _CPBase(discord.ui.Button):
    """CPViewの全ボタン共通基底クラス"""
    def __init__(self, label, style, row, view_ref):
        super().__init__(label=label, style=style, row=row)
        self._view = view_ref

class _CPNavButton(discord.ui.Button):
    """ページ切替ボタン"""
    def __init__(self, label, delta, view_ref):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self._delta    = delta
        self._view_ref = view_ref
    async def callback(self, interaction):
        self._view_ref.page += self._delta
        self._view_ref._build_buttons()
        await interaction.response.edit_message(
            embed=self._view_ref._make_embed(), view=self._view_ref)

class _ToggleButton(discord.ui.Button):
    """えっち検出/川柳検出のON/OFFボタン"""
    def __init__(self, *, label, style, row, guild_id, feature, scope, on):
        super().__init__(label=label, style=style, row=row)
        self.guild_id = guild_id; self.feature = feature
        self.scope = scope; self.on = on
    async def callback(self, interaction):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("チャンネル管理権限が必要です。", ephemeral=True); return
        gd = db_read(self.feature, guild_id=self.guild_id)
        if self.scope == "server":
            gd["server"] = self.on
        else:
            chs = gd.get("channels", [])
            if self.on and interaction.channel_id not in chs: chs.append(interaction.channel_id)
            elif not self.on and interaction.channel_id in chs: chs.remove(interaction.channel_id)
            gd["channels"] = chs
        db_write(self.feature, gd, guild_id=self.guild_id)
        scope_txt = "サーバー全体" if self.scope == "server" else "このチャンネル"
        if self.feature == "haiku":
            feat_txt = "川柳検出"
        elif self.feature == "atsumori":
            feat_txt = "熱盛検知"
        elif self.feature == "lewd":
            feat_txt = "えっち検出"
        elif self.feature == "romaji":
            feat_txt = "ローマ字翻訳"
        elif self.feature == "impersonate":
            feat_txt = "なりすまし機能"
        elif self.feature == "akeome":
            feat_txt = "あけおめ機能"
        else:
            feat_txt = "機能"
        await interaction.response.send_message(
            f"{scope_txt}の{feat_txt}を {'ON' if self.on else 'OFF'} にしました。", ephemeral=True)

# ページ0〜3の各ボタン（全てサブクラス化）
class _BtnSetWelcome(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="歓迎メッセージ設定", style=discord.ButtonStyle.primary, row=0); self.gid=gid
    async def callback(self, i): await i.response.send_modal(WelcomeSetModal(self.gid))

class _BtnSetGoodbye(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="送別メッセージ設定", style=discord.ButtonStyle.primary, row=0); self.gid=gid
    async def callback(self, i): await i.response.send_modal(GoodbyeSetModal(self.gid))

class _BtnAddWord(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="禁止ワード追加", style=discord.ButtonStyle.danger, row=1); self.gid=gid
    async def callback(self, i): await i.response.send_modal(WordAddModal(self.gid))

class _BtnListWords(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="禁止ワード一覧", style=discord.ButtonStyle.secondary, row=1); self.gid=gid
    async def callback(self, i):
        gd    = db_read("wordblock", guild_id=self.gid)
        words = gd.get("words", [])
        text  = "\n".join(f"• {w}" for w in words) or "なし"
        await i.response.send_message(f"禁止ワード:\n{text}", ephemeral=True)
class _BtnAddAutoreply(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="自動返信追加", style=discord.ButtonStyle.success, row=2); self.gid=gid
    async def callback(self, i): await i.response.send_modal(AutoreplyAddModal(self.gid))

class _BtnListAutoreply(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="自動返信一覧", style=discord.ButtonStyle.secondary, row=2); self.gid=gid
    async def callback(self, i):
        gd = db_read("autoreply", guild_id=self.gid)
        ar = gd.get("replies", {})
        text = "\n".join(f"• `{k}` → {v['text']}" for k,v in ar.items()) or "なし"
        await i.response.send_message(f"自動返信:\n{text}", ephemeral=True)

class _BtnPreviewWelcome(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="歓迎メッセージ プレビュー", style=discord.ButtonStyle.secondary, row=3); self.gid=gid
    async def callback(self, i):
        gd  = db_read("welcome", guild_id=self.gid)
        msg = gd.get("message", "未設定")
        pre = msg.replace("{user}", i.user.mention).replace("{members}", str(i.guild.member_count))
        await i.response.send_message(f"**プレビュー:**\n{pre}", ephemeral=True)

class _BtnPreviewGoodbye(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="送別メッセージ プレビュー", style=discord.ButtonStyle.secondary, row=3); self.gid=gid
    async def callback(self, i):
        gd  = db_read("goodbye", guild_id=self.gid)
        msg = gd.get("message", "未設定")
        pre = msg.replace("{user}", i.user.display_name).replace("{members}", str(i.guild.member_count))
        await i.response.send_message(f"**プレビュー:**\n{pre}", ephemeral=True)

# ページ1（機能ON/OFF）はすでに_ToggleButtonで実装済み

class _BtnResource(discord.ui.Button):
    def __init__(self): super().__init__(label="リソース確認", style=discord.ButtonStyle.secondary, row=0)
    async def callback(self, i):
        await i.response.defer(ephemeral=True)
        embed = await build_resource_embed(i.client)
        await i.followup.send(embed=embed, ephemeral=True)

class _BtnPermission(discord.ui.Button):
    def __init__(self): super().__init__(label="権限確認", style=discord.ButtonStyle.secondary, row=0)
    async def callback(self, i):
        await i.response.defer(ephemeral=True)
        me = i.guild.me; perms = me.guild_permissions
        checks = [("管理者",perms.administrator),("チャンネル管理",perms.manage_channels),
                  ("ロール管理",perms.manage_roles),("メッセージ管理",perms.manage_messages),
                  ("サーバー管理",perms.manage_guild),("Webhook管理",perms.manage_webhooks),
                  ("メンバー管理",perms.manage_members if hasattr(perms,'manage_members') else False),
                  ("ロール付与",perms.manage_roles)]
        ok = [n for n,v in checks if v]; ng = [n for n,v in checks if not v]
        embed = discord.Embed(title="Bot権限", color=0x57F287 if not ng else 0xED4245)
        embed.add_field(name=f"OK ({len(ok)})", value="\n".join(ok) or "なし", inline=True)
        embed.add_field(name=f"NG ({len(ng)})", value="\n".join(ng) or "なし", inline=True)
        embed.add_field(name="Ping", value=f"{round(i.client.latency*1000,1)}ms", inline=False)
        await i.followup.send(embed=embed, ephemeral=True)

class _BtnBackup(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="バックアップ情報", style=discord.ButtonStyle.secondary, row=1); self.gid=gid
    async def callback(self, i):
        bk = db_read("backup", guild_id=self.gid)
        if bk:
            embed = discord.Embed(title="バックアップ情報", color=0x57F287)
            embed.add_field(name="保存日時", value=bk.get("saved_at","不明"), inline=False)
            embed.add_field(name="コード",   value=f"`{bk.get('code','不明')}`", inline=True)
            embed.add_field(name="ロール数", value=str(len(bk.get("roles",[]))), inline=True)
            embed.add_field(name="ch数",     value=str(len(bk.get("channels",[]))), inline=True)
        else:
            embed = discord.Embed(title="バックアップなし", description="/save で作成できます。", color=0xED4245)
        await i.response.send_message(embed=embed, ephemeral=True)

class _BtnSettings(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="現在の設定一覧", style=discord.ButtonStyle.primary, row=1); self.gid=gid
    async def callback(self, i):
        wd = db_read("welcome",  guild_id=self.gid)
        gd = db_read("goodbye",  guild_id=self.gid)
        wb = db_read("wordblock",guild_id=self.gid)
        ar = db_read("autoreply",guild_id=self.gid)
        hk = db_read("haiku",    guild_id=self.gid)
        lw = db_read("lewd",     guild_id=self.gid)
        am = db_read("atsumori", guild_id=self.gid)
        wch = wd.get("channel"); fch = gd.get("channel")
        ai_chats = sum(1 for cid in getattr(bot, "_active_chats", {}) if i.guild.get_channel(cid))
        lines = [
            f"歓迎ch: {i.guild.get_channel(wch).mention if wch and i.guild.get_channel(wch) else '未設定'}",
            f"送別ch: {i.guild.get_channel(fch).mention if fch and i.guild.get_channel(fch) else '未設定'}",
            f"禁止ワード: {len(wb.get('words',[]))}件",
            f"自動返信: {len(ar.get('replies',{}))}件",
            f"川柳検出ch: {len(hk.get('channels',[]))}件" + (" +全体" if hk.get("server") else ""),
            f"えっち検出ch: {len(lw.get('channels',[]))}件" + (" +全体" if lw.get("server") else ""),
            f"熱盛検知ch: {len(am.get('channels',[]))}件" + (" +全体" if am.get("server") else ""),
            f"AI会話稼働ch: {ai_chats}件",
        ]
        embed = discord.Embed(title="現在の設定一覧", description="\n".join(lines), color=0x5865F2)
        await i.response.send_message(embed=embed, ephemeral=True)

class _BtnCreateVerify(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="認証パネル作成", style=discord.ButtonStyle.success, row=2); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(VerifySetModal(self.gid, self.cid))

class _BtnCreateRolePanel(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="ロールパネル作成", style=discord.ButtonStyle.success, row=2); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(RolePanelModal(self.gid, self.cid))

class _BtnEditRolePanel(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="ロールパネル編集", style=discord.ButtonStyle.secondary, row=2); self.gid=gid; self.cid=cid
    async def callback(self, i):
        if not i.user.guild_permissions.manage_roles:
            await i.response.send_message("ロール管理権限が必要です。", ephemeral=True); return
        await i.response.send_modal(RolePanelEditModal(self.gid, self.cid))

class _BtnGlobalChat(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="グローバルチャット", style=discord.ButtonStyle.primary, row=3); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(GlobalChatModal(self.gid, self.cid))

class _BtnPurge(discord.ui.Button):
    def __init__(self, ch): super().__init__(label="メッセージ一括削除", style=discord.ButtonStyle.danger, row=3); self.ch=ch
    async def callback(self, i):
        if not i.user.guild_permissions.manage_messages:
            await i.response.send_message("メッセージ管理権限が必要です。", ephemeral=True); return
        await i.response.send_modal(PurgeModal(self.ch))

# ── AIチャット関連ボタン表示用 ──────────────────────
class _BtnStartAIChat(discord.ui.Button):
    def __init__(self, gid, cid):
        super().__init__(label="AIチャット ON (このch)", style=discord.ButtonStyle.success, row=0)
        self.cid = cid
    async def callback(self, i):
        if self.cid in getattr(bot, "_active_chats", {}):
            await i.response.send_message("すでに稼働中です", ephemeral=True)
            return
        chars = SCENARIOS.get("kouma")
        bot._active_chats[self.cid] = {"chars": chars, "scenario_name": "kouma", "topic": "自由な雑談", "history": [], "task": None}
        bot._active_chats[self.cid]["history"].append({"name": "System", "content": "新しい話題「自由な雑談」について会話を開始しました。"})
        bot._active_chats[self.cid]["task"] = asyncio.create_task(_chat_loop(self.cid))
        _save_active_chats()
        await i.response.send_message("AIチャットを開始しました (紅魔郷 / 話題: 自由な雑談)", ephemeral=True)

class _BtnStopAIChat(discord.ui.Button):
    def __init__(self, gid, cid):
        super().__init__(label="AIチャット OFF (このch)", style=discord.ButtonStyle.danger, row=0)
        self.cid = cid
    async def callback(self, i):
        session = getattr(bot, "_active_chats", {}).pop(self.cid, None)
        if session:
            try: session["task"].cancel()
            except: pass
            _save_active_chats()
            try:
                ch = bot.get_channel(self.cid)
                if ch:
                    for wh in await ch.webhooks():
                        if wh.name == "MamechosuChat":
                            await wh.delete()
                            break
            except Exception:
                pass
            await i.response.send_message("AIチャットを終了しました", ephemeral=True)
        else:
            await i.response.send_message("AIチャットは稼働中ではありません", ephemeral=True)

class AICustomKeyModal(discord.ui.Modal, title="サーバー独自APIキー設定（全AI機能共通）"):
    api_key = discord.ui.TextInput(label="Groq APIキー", placeholder="gsk_...", required=True)
    def __init__(self, gid):
        super().__init__()
        self.gid = gid
    async def on_submit(self, interaction: discord.Interaction):
        settings = db_read("aichat_settings", str(self.gid))
        if not isinstance(settings, dict): settings = {}
        settings["custom_api_key"] = self.api_key.value
        db_write("aichat_settings", settings, guild_id=self.gid)
        await interaction.response.send_message("サーバー独自のAPIキーを保存しました。このサーバーのAI機能すべてに適用されます。", ephemeral=True)

class AIFrequencyModal(discord.ui.Modal, title="AIチャット会話頻度設定"):
    freq_min = discord.ui.TextInput(label="最小間隔 (分)", placeholder="10", required=True)
    freq_max = discord.ui.TextInput(label="最大間隔 (分)", placeholder="20", required=True)
    def __init__(self, gid):
        super().__init__()
        self.gid = gid
        settings = db_read("aichat_settings", str(self.gid))
        if isinstance(settings, dict):
            self.freq_min.default = str(settings.get("interval_min", 10))
            self.freq_max.default = str(settings.get("interval_max", 20))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            vmin = int(self.freq_min.value)
            vmax = int(self.freq_max.value)
            if vmin < 1 or vmax < 1 or vmin > vmax: raise ValueError
        except:
            await interaction.response.send_message("正しい数値を入力してください(最小<=最大)。", ephemeral=True)
            return
        settings = db_read("aichat_settings", str(self.gid))
        if not isinstance(settings, dict): settings = {}
        settings["interval_min"] = vmin
        settings["interval_max"] = vmax
        db_write("aichat_settings", settings, guild_id=self.gid)
        await interaction.response.send_message(f"会話頻度を {vmin}分〜{vmax}分 に設定しました。", ephemeral=True)

class _BtnSetAICustomKey(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="APIキー設定(全AI機能共通)", style=discord.ButtonStyle.secondary, row=1)
        self.gid = gid
    async def callback(self, i):
        await i.response.send_modal(AICustomKeyModal(self.gid))

class _BtnRemoveAICustomKey(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="APIキー削除", style=discord.ButtonStyle.secondary, row=1)
        self.gid = gid
    async def callback(self, i):
        settings = db_read("aichat_settings", str(self.gid))
        if isinstance(settings, dict) and "custom_api_key" in settings:
            del settings["custom_api_key"]
            db_write("aichat_settings", settings, guild_id=self.gid)
            await i.response.send_message("カスタムAPIキーを削除し、デフォルトに戻しました。", ephemeral=True)
        else:
            await i.response.send_message("カスタムAPIキーは設定されていません。", ephemeral=True)

class _BtnSetAIFrequency(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="会話頻度設定", style=discord.ButtonStyle.primary, row=2)
        self.gid = gid
    async def callback(self, i):
        await i.response.send_modal(AIFrequencyModal(self.gid))

class ImpersonateChanceModal(discord.ui.Modal, title="なりすましバレ確率設定"):
    percent = discord.ui.TextInput(
        label="バレる確率 (0〜100)",
        placeholder="例: 10",
        default="10",
        required=True
    )
    def __init__(self, gid):
        super().__init__()
        self.gid = gid
        gd = db_read("impersonate", guild_id=gid)
        self.percent.default = str(gd.get("expose_rate", 10))
    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.percent.value)
            if not 0 <= val <= 100:
                raise ValueError
        except:
            await interaction.response.send_message("確率は 0〜100 の整数で指定してください。", ephemeral=True)
            return
        gd = db_read("impersonate", guild_id=self.gid)
        gd["expose_rate"] = val
        db_write("impersonate", gd, guild_id=self.gid)
        await interaction.response.send_message(f"なりすましのバレ確率を {val}% に設定しました。", ephemeral=True)

class _BtnSetImpersonateChance(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="バレ確率変更", style=discord.ButtonStyle.secondary, row=3)
        self.gid = gid
    async def callback(self, i):
        await i.response.send_modal(ImpersonateChanceModal(self.gid))

class _BtnImpersonateLog(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="なりすましログ確認", style=discord.ButtonStyle.primary, row=4)
        self.gid = gid
    async def callback(self, i):
        import datetime
        log_data = db_read("impersonate_log", guild_id=self.gid)
        if not log_data:
            await i.response.send_message("なりすましの履歴はありません。", ephemeral=True)
            return
        embed = discord.Embed(title="なりすましログ（過去30日）", color=0x5865F2)
        desc = ""
        for log in reversed(log_data[-10:]):
            dt = datetime.datetime.fromtimestamp(log["timestamp"]).strftime("%Y/%m/%d %H:%M")
            desc += f"`{dt}` **{log['executor']}** → **{log['target']}**\n"
            desc += f"> {log['content'][:30]}\n"
            desc += f"[リンク]({log['url']})\n\n"
        embed.description = desc
        await i.response.send_message(embed=embed, ephemeral=True)

class _BtnCPHelp(discord.ui.Button):
    def __init__(self):
        super().__init__(label="CPヘルプ", style=discord.ButtonStyle.primary, row=3)
    async def callback(self, i):
        text = (
            "**【コントロールパネル(CP)のヘルプ】**\n"
            "`/cp` コマンドを実行すると、サーバーの各種機能を設定できるパネルが表示されます。\n\n"
            "**主な機能**:\n"
            "- **メッセージ・ワード管理**: 参加・退出メッセージ、禁止ワード、自動返信の設定\n"
            "- **川柳 / えっち検出 / 熱盛検知**: 各種お遊び機能のON/OFF\n"
            "- **ローマ字翻訳 / なりすまし**: ローマ字の自動翻訳、なりすまし機能のON/OFFやバレ確率の設定\n"
            "- **サーバー情報等**: バックアップ、ロールパネル作成など\n"
            "- **AIチャット**: チャンネル指定でAIと会話する機能\n\n"
            "※CPの操作には「チャンネルの管理」権限が必要です。"
        )
        await i.response.send_message(text, ephemeral=True)

# ── CPView (6ページ構成) ──────────────────────
class CPView(discord.ui.View):
    PAGE_TITLES = [
        "メッセージ・ワード管理",
        "川柳 ON/OFF",
        "えっち検出 ON/OFF",
        "熱盛検知 ON/OFF",
        "ローマ字翻訳 / なりすまし ON/OFF",
        "サーバー情報・バックアップ / パネル作成",
        "AIチャット ON/OFF",
    ]
    MAX_PAGE = 6

    def __init__(self, guild_id: int, channel_id: int, page: int = 0):
        super().__init__(timeout=300)
        self.guild_id   = guild_id
        self.channel_id = channel_id
        self.page       = page
        self._build_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "コントロールパネルはチャンネル管理権限が必要です。", ephemeral=True)
            return False
        return True

    def _build_buttons(self):
        self.clear_items()
        gid = self.guild_id
        cid = self.channel_id
        p   = self.page

        if p == 0:
            # ページ1: メッセージ・ワード管理
            self.add_item(_BtnSetWelcome(gid));     self.add_item(_BtnSetGoodbye(gid))
            self.add_item(_BtnPreviewWelcome(gid)); self.add_item(_BtnPreviewGoodbye(gid))
            self.add_item(_BtnAddWord(gid));        self.add_item(_BtnListWords(gid))
            self.add_item(_BtnAddAutoreply(gid));   self.add_item(_BtnListAutoreply(gid))
            self.add_item(_BtnCPHelp())

        elif p == 1:
            # ページ2: 川柳 ON/OFF (このch / 全体)
            specs = [
                ("川柳 ON  (このch)", "haiku", "channel", True,  discord.ButtonStyle.success, 0),
                ("川柳 OFF (このch)", "haiku", "channel", False, discord.ButtonStyle.danger,  0),
                ("川柳 ON  (全体)",   "haiku", "server",  True,  discord.ButtonStyle.success, 1),
                ("川柳 OFF (全体)",   "haiku", "server",  False, discord.ButtonStyle.danger,  1),
            ]
            for label, feat, scope, on, style, row in specs:
                self.add_item(_ToggleButton(label=label, style=style, row=row,
                                            guild_id=gid, feature=feat, scope=scope, on=on))

        elif p == 2:
            # ページ3: えっち検出 ON/OFF (このch / 全体)
            specs = [
                ("えっち ON  (このch)", "lewd", "channel", True,  discord.ButtonStyle.success, 0),
                ("えっち OFF (このch)", "lewd", "channel", False, discord.ButtonStyle.danger,  0),
                ("えっち ON  (全体)",   "lewd", "server",  True,  discord.ButtonStyle.success, 1),
                ("えっち OFF (全体)",   "lewd", "server",  False, discord.ButtonStyle.danger,  1),
            ]
            for label, feat, scope, on, style, row in specs:
                self.add_item(_ToggleButton(label=label, style=style, row=row,
                                            guild_id=gid, feature=feat, scope=scope, on=on))

        elif p == 3:
            # ページ4: 熱盛検知 ON/OFF (このch / 全体)
            specs = [
                ("熱盛 ON  (このch)", "atsumori", "channel", True,  discord.ButtonStyle.success, 0),
                ("熱盛 OFF (このch)", "atsumori", "channel", False, discord.ButtonStyle.danger,  0),
                ("熱盛 ON  (全体)",   "atsumori", "server",  True,  discord.ButtonStyle.success, 1),
                ("熱盛 OFF (全体)",   "atsumori", "server",  False, discord.ButtonStyle.danger,  1),
            ]
            for label, feat, scope, on, style, row in specs:
                self.add_item(_ToggleButton(label=label, style=style, row=row,
                                            guild_id=gid, feature=feat, scope=scope, on=on))

        elif p == 4:
            # ページ5: ローマ字翻訳 / なりすまし / あけおめ ON/OFF
            specs = [
                ("ローマ字 ON  (このch)", "romaji", "channel", True,  discord.ButtonStyle.success, 0),
                ("ローマ字 OFF (このch)", "romaji", "channel", False, discord.ButtonStyle.danger,  0),
                ("ローマ字 ON  (全体)",   "romaji", "server",  True,  discord.ButtonStyle.success, 1),
                ("ローマ字 OFF (全体)",   "romaji", "server",  False, discord.ButtonStyle.danger,  1),
                ("なりすまし ON  (このch)", "impersonate", "channel", True,  discord.ButtonStyle.success, 2),
                ("なりすまし OFF (このch)", "impersonate", "channel", False, discord.ButtonStyle.danger,  2),
                ("あけおめ ON  (このch)", "akeome", "channel", True,  discord.ButtonStyle.success, 3),
                ("あけおめ OFF (このch)", "akeome", "channel", False, discord.ButtonStyle.danger,  3),
            ]
            for label, feat, scope, on, style, row in specs:
                self.add_item(_ToggleButton(label=label, style=style, row=row,
                                            guild_id=gid, feature=feat, scope=scope, on=on))
            self.add_item(_BtnSetImpersonateChance(gid))
            self.add_item(_BtnImpersonateLog(gid))

        elif p == 5:
            # ページ6: サーバー情報・バックアップ / パネル作成
            self.add_item(_BtnResource());         self.add_item(_BtnPermission())
            self.add_item(_BtnBackup(gid));        self.add_item(_BtnSettings(gid))
            ch = bot.get_channel(cid)
            self.add_item(_BtnCreateVerify(gid, cid))
            self.add_item(_BtnCreateRolePanel(gid, cid))
            self.add_item(_BtnEditRolePanel(gid, cid))
            self.add_item(_BtnGlobalChat(gid, cid))
            if ch:
                self.add_item(_BtnPurge(ch))
        
        elif p == 6:
            # ページ7: AIチャット ON/OFF
            self.add_item(_BtnStartAIChat(gid, cid))
            self.add_item(_BtnStopAIChat(gid, cid))
            self.add_item(_BtnSetAICustomKey(gid))
            self.add_item(_BtnRemoveAICustomKey(gid))
            self.add_item(_BtnSetAIFrequency(gid))

        # ナビボタン (row=4)
        if p > 0:
            self.add_item(_CPNavButton("← 前へ", -1, self))
        if p < self.MAX_PAGE:
            self.add_item(_CPNavButton("次へ →", +1, self))

    def _make_embed(self):
        return discord.Embed(
            title=(f"コントロールパネル [{self.page+1}/{self.MAX_PAGE+1}]"
                   f"  {self.PAGE_TITLES[self.page]}"),
            description="ボタンで各機能を操作できます。",
            color=0xFEE75C)

@bot.tree.command(name="cp_help", description="コントロールパネル(CP)のヘルプを表示します")
@app_commands.default_permissions(manage_channels=True)
async def cmd_cp_help(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.response.send_message("チャンネル管理権限が必要です。", ephemeral=True)
        return
    text = (
        "**【コントロールパネル(CP)のヘルプ】**\n"
        "`/cp` コマンドを実行すると、サーバーの各種機能を設定できるパネルが表示されます。\n\n"
        "**主な機能**:\n"
        "- **メッセージ・ワード管理**: 参加・退出メッセージ、禁止ワード、自動返信の設定\n"
        "- **川柳 / えっち検出 / 熱盛検知**: 各種お遊び機能のON/OFF\n"
        "- **ローマ字翻訳 / なりすまし**: ローマ字の自動翻訳、なりすまし機能のON/OFFやバレ確率の設定\n"
        "- **サーバー情報等**: バックアップ、ロールパネル作成など\n"
        "- **AIチャット**: チャンネル指定でAIと会話する機能\n\n"
        "※CPの操作には「チャンネルの管理」権限が必要です。"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="cp", description="コントロールパネルを開きます")
@app_commands.default_permissions(manage_channels=True)
async def cmd_cp(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("コントロールパネルはチャンネル管理権限が必要です。", ephemeral=True)
        return
    view  = CPView(interaction.guild_id, interaction.channel_id)
    embed = view._make_embed()
    await interaction.followup.send(embed=embed, view=view)

# ──────────────────────────────────────────────
# 5. ロールパネル（リアクション式）
# ──────────────────────────────────────────────
class DMPasswordModal(discord.ui.Modal, title="パスワード入力"):
    pw = discord.ui.TextInput(label="パスワード", required=True)
    def __init__(self, guild_id: int, role_id: int, correct_pw: str):
        super().__init__()
        self.guild_id = guild_id
        self.role_id = role_id
        self.correct_pw = correct_pw
    async def on_submit(self, interaction: discord.Interaction):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("サーバーが見つかりません。", ephemeral=True); return
        member = guild.get_member(interaction.user.id)
        if not member:
            await interaction.response.send_message("サーバーに参加していません。", ephemeral=True); return
        if not _check_password_attempt(interaction.user.id, self.role_id):
            await interaction.response.send_message("試行回数が多すぎます。60秒後に再試行してください。", ephemeral=True); return
        if self.pw.value == self.correct_pw:
            _clear_password_attempt(interaction.user.id, self.role_id)
            role = guild.get_role(self.role_id)
            if role:
                try:
                    if role in member.roles:
                        await member.remove_roles(role)
                        await interaction.response.send_message(f"サーバー「{guild.name}」で {role.name} を外しました。", ephemeral=True)
                    else:
                        await member.add_roles(role)
                        await interaction.response.send_message(f"サーバー「{guild.name}」で {role.name} を付与しました。", ephemeral=True)
                except Exception as e:
                    await interaction.response.send_message(f"エラー: {e}", ephemeral=True)
            else:
                await interaction.response.send_message("ロールが見つかりません。", ephemeral=True)
        else:
            await interaction.response.send_message("パスワードが違います。", ephemeral=True)

class DMPasswordView(discord.ui.View):
    def __init__(self, guild_id: int, role_id: int, correct_pw: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.role_id = role_id
        self.correct_pw = correct_pw
        
    @discord.ui.button(label="パスワードを入力", style=discord.ButtonStyle.primary)
    async def btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DMPasswordModal(self.guild_id, self.role_id, self.correct_pw))

@bot.tree.command(name="rolepanel", description="ロールパネルを作成します")
@app_commands.describe(roles_and_emojis="例: 🍎:@Role1, 🍇:@Role2", title="タイトル", password="パスワード（省略可）")
async def cmd_rolepanel(interaction: discord.Interaction, roles_and_emojis: str, title: str = "ロールパネル", password: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("ロール管理権限が必要です。", ephemeral=True)
        return
        
    mapping = parse_roles_and_emojis(interaction.guild, roles_and_emojis)
    if not mapping:
        await interaction.followup.send("有効な「絵文字:ロール」のペアが見つかりません。例: `🍎:@Role1`", ephemeral=True)
        return
        
    embed = _build_rolepanel_embed(interaction.guild, title, mapping, password)
    msg = await interaction.channel.send(embed=embed)

    db_write("reaction_roles", {"guild_id": interaction.guild_id, "channel_id": interaction.channel_id, "title": title,
                                 "roles": mapping, "password": password}, shared=str(msg.id))
    failed = await _add_rolepanel_reactions(msg, mapping)

    await interaction.followup.send("ロールパネルを作成しました。" + _rolepanel_failed_warning(failed), ephemeral=True)

@bot.tree.command(name="rolepaneledit", description="既存のロールパネルを編集します")
@app_commands.describe(message_id="編集するパネルのメッセージID", roles_and_emojis="例: 🍎:@Role1, 🍇:@Role2",
                        title="タイトル（省略時は現在のタイトルを維持）", password="パスワード（省略時は現在の設定を維持）",
                        remove_password="パスワード保護を解除する")
async def cmd_rolepaneledit(interaction: discord.Interaction, message_id: str, roles_and_emojis: str,
                             title: str = None, password: str = None, remove_password: bool = False):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("ロール管理権限が必要です。", ephemeral=True)
        return

    try:
        mid = int(message_id.strip())
    except ValueError:
        await interaction.followup.send("メッセージIDは数字で指定してください。", ephemeral=True)
        return

    panel_data = db_read("reaction_roles", shared=str(mid))
    if not panel_data or not isinstance(panel_data, dict) or panel_data.get("guild_id") != interaction.guild_id:
        await interaction.followup.send("指定したメッセージIDのロールパネルが見つかりません。", ephemeral=True)
        return

    ch_id = panel_data.get("channel_id") or interaction.channel_id
    channel = interaction.guild.get_channel_or_thread(ch_id) or interaction.channel
    try:
        msg = await channel.fetch_message(mid)
    except Exception:
        await interaction.followup.send("パネルのメッセージが見つかりません（削除された可能性があります）。", ephemeral=True)
        return

    mapping = parse_roles_and_emojis(interaction.guild, roles_and_emojis)
    if not mapping:
        await interaction.followup.send("有効な「絵文字:ロール」のペアが見つかりません。例: `🍎:@Role1`", ephemeral=True)
        return

    new_title = title if title is not None else panel_data.get("title", "ロールパネル")
    if remove_password:
        new_pw = None
    elif password is not None:
        new_pw = password
    else:
        new_pw = panel_data.get("password")

    embed = _build_rolepanel_embed(interaction.guild, new_title, mapping, new_pw)
    try:
        await msg.edit(embed=embed)
        await msg.clear_reactions()
    except Exception as e:
        await interaction.followup.send(f"パネルの更新に失敗しました: {e}", ephemeral=True)
        return

    failed = await _add_rolepanel_reactions(msg, mapping)

    db_write("reaction_roles", {"guild_id": interaction.guild_id, "channel_id": channel.id, "title": new_title,
                                 "roles": mapping, "password": new_pw}, shared=str(mid))
    await interaction.followup.send("ロールパネルを編集しました。" + _rolepanel_failed_warning(failed), ephemeral=True)

# ──────────────────────────────────────────────
# 6. 歓迎・送別メッセージ
# ──────────────────────────────────────────────
@bot.tree.command(name="welcome", description="歓迎メッセージを設定します")
@app_commands.describe(action="set / preview / off", channel="送信先チャンネル", message="{user}/{members}が使えます")
async def cmd_welcome(interaction: discord.Interaction, action: str, channel: discord.TextChannel = None, message: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("サーバー管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("welcome", guild_id=interaction.guild_id)
    if action == "set":
        if not channel or not message:
            await interaction.followup.send("チャンネルとメッセージを指定してください。", ephemeral=True)
            return
        gd["channel"] = channel.id
        gd["message"] = message
        db_write("welcome", gd, guild_id=interaction.guild_id)
        await interaction.followup.send(f"歓迎メッセージを設定しました → {channel.mention}", ephemeral=True)
    elif action == "preview":
        msg = gd.get("message", "未設定")
        preview = msg.replace("{user}", interaction.user.mention).replace("{members}", str(interaction.guild.member_count))
        await interaction.followup.send(f"プレビュー:\n{preview}", ephemeral=True)
    elif action == "off":
        db_write("welcome", {}, guild_id=interaction.guild_id)
        await interaction.followup.send("歓迎メッセージを無効化しました。", ephemeral=True)

@bot.tree.command(name="goodbye", description="送別メッセージを設定します")
@app_commands.describe(action="set / preview / off", channel="送信先チャンネル", message="{user}/{members}が使えます")
async def cmd_goodbye(interaction: discord.Interaction, action: str, channel: discord.TextChannel = None, message: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("サーバー管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("goodbye", guild_id=interaction.guild_id)
    if action == "set":
        if not channel or not message:
            await interaction.followup.send("チャンネルとメッセージを指定してください。", ephemeral=True)
            return
        gd["channel"] = channel.id
        gd["message"] = message
        db_write("goodbye", gd, guild_id=interaction.guild_id)
        await interaction.followup.send(f"送別メッセージを設定しました → {channel.mention}", ephemeral=True)
    elif action == "preview":
        msg = gd.get("message", "未設定")
        preview = msg.replace("{user}", interaction.user.display_name).replace("{members}", str(interaction.guild.member_count))
        await interaction.followup.send(f"プレビュー:\n{preview}", ephemeral=True)
    elif action == "off":
        db_write("goodbye", {}, guild_id=interaction.guild_id)
        await interaction.followup.send("送別メッセージを無効化しました。", ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not bot.user or payload.user_id == bot.user.id:
        return

    try:
        panel_data = db_read("reaction_roles", shared=str(payload.message_id))
        if not panel_data or not isinstance(panel_data, dict):
            return

        guild = bot.get_guild(payload.guild_id)
        if not guild:
            try: guild = await bot.fetch_guild(payload.guild_id)
            except Exception: return
        if not guild: return

        member = guild.get_member(payload.user_id)
        if not member:
            try: member = await guild.fetch_member(payload.user_id)
            except Exception: return
        if not member or member.bot: return

        emoji_str = str(payload.emoji)
        roles = panel_data.get("roles", {})

        ch = guild.get_channel_or_thread(payload.channel_id)
        if not ch:
            try: ch = await bot.fetch_channel(payload.channel_id)
            except Exception: ch = None

        if emoji_str not in roles:
            if ch:
                try:
                    msg = await ch.fetch_message(payload.message_id)
                    await msg.remove_reaction(payload.emoji, member)
                except Exception:
                    pass
            return

        role_id = roles[emoji_str]
        pw = panel_data.get("password")

        msg = None
        if ch:
            try:
                msg = await ch.fetch_message(payload.message_id)
                await msg.remove_reaction(payload.emoji, member)
            except Exception:
                pass

        role = guild.get_role(role_id)
        if not role: return

        if role >= guild.me.top_role:
            if ch:
                try:
                    await ch.send(f"{member.mention} 権限エラー: Botのロール ({guild.me.top_role.name}) より上位のロールは操作できません。", delete_after=10)
                except Exception:
                    pass
            return

        if pw:
            try:
                view = DMPasswordView(guild.id, role_id, pw)
                await member.send(f"サーバー「{guild.name}」のロール **{role.name}** を取得/解除するにはパスワードが必要です。", view=view)
            except discord.Forbidden:
                if ch:
                    try:
                        await ch.send(f"{member.mention} DMを送信できませんでした。サーバーからのDMを許可設定にしてもう一度お試しください。", delete_after=10)
                    except Exception:
                        pass
        else:
            try:
                if role in member.roles:
                    await member.remove_roles(role)
                    if ch:
                        try: await ch.send(f"{member.mention} サーバー「{guild.name}」で **{role.name}** を外しました。", delete_after=5)
                        except Exception: pass
                else:
                    await member.add_roles(role)
                    if ch:
                        try: await ch.send(f"{member.mention} サーバー「{guild.name}」で **{role.name}** を付与しました。", delete_after=5)
                        except Exception: pass
                if ch and msg:
                    embed = _build_rolepanel_embed(guild, panel_data.get("title", "ロールパネル"), roles, pw)
                    try: await msg.edit(embed=embed)
                    except Exception: pass
            except Exception as e:
                db_log("rolepanel_role_change_failed", str(e), level="ERROR")
    except Exception as e:
        db_log("rolepanel_reaction_failed", str(e), level="ERROR")

@bot.event
async def on_member_join(member: discord.Member):
    gd = db_read("welcome", guild_id=member.guild.id)
    ch_id = gd.get("channel")
    msg   = gd.get("message")
    if ch_id and msg:
        ch = member.guild.get_channel(ch_id)
        if ch:
            await ch.send(msg.replace("{user}", member.mention).replace("{members}", str(member.guild.member_count)))

@bot.event
async def on_member_remove(member: discord.Member):
    gd = db_read("goodbye", guild_id=member.guild.id)
    ch_id = gd.get("channel")
    msg   = gd.get("message")
    if ch_id and msg:
        ch = member.guild.get_channel(ch_id)
        if ch:
            await ch.send(msg.replace("{user}", member.display_name).replace("{members}", str(member.guild.member_count)))


# ──────────────────────────────────────────────
# 7. 禁止ワード /wordblock
# ──────────────────────────────────────────────
class WordblockRemoveView(discord.ui.View):
    def __init__(self, words: list[str], guild_id: int):
        super().__init__(timeout=30)
        self.words    = words
        self.guild_id = guild_id
        # 選択肢をSelectMenuで表示（最大25件）
        options = [discord.SelectOption(label=w, value=w) for w in words[:25]]
        options.append(discord.SelectOption(label="[全て削除]", value="__all__"))
        self.add_item(WordblockSelect(options, guild_id))

class WordblockSelect(discord.ui.Select):
    def __init__(self, options, guild_id):
        super().__init__(placeholder="削除するワードを選択...", options=options, min_values=1, max_values=1)
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        gd = db_read("wordblock", guild_id=self.guild_id)
        blocked = gd.get("words", [])
        choice = self.values[0]
        if choice == "__all__":
            gd["words"] = []
            msg = "禁止ワードを全て削除しました。"
        else:
            if choice in blocked:
                blocked.remove(choice)
            gd["words"] = blocked
            msg = f"`{choice}` を削除しました。"
        db_write("wordblock", gd, guild_id=self.guild_id)
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="wordblock", description="禁止ワード/絵文字を管理します")
@app_commands.describe(action="add / remove / list", word="追加するワード（removeは省略可）")
async def cmd_wordblock(interaction: discord.Interaction, action: str, word: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("メッセージ管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("wordblock", guild_id=interaction.guild_id)
    blocked = gd.get("words", [])
    if action == "add":
        if not word:
            await interaction.followup.send("ワードを指定してください。", ephemeral=True)
            return
        if word not in blocked:
            blocked.append(word)
        gd["words"] = blocked
        db_write("wordblock", gd, guild_id=interaction.guild_id)
        await interaction.followup.send(f"`{word}` を禁止リストに追加しました。", ephemeral=True)
    elif action == "remove":
        if not blocked:
            await interaction.followup.send("禁止ワードがありません。", ephemeral=True)
            return
        view = WordblockRemoveView(blocked, interaction.guild_id)
        await interaction.followup.send("削除するワードを選択してください:", view=view, ephemeral=True)
    elif action == "list":
        text = "\n".join(blocked) if blocked else "なし"
        await interaction.followup.send(f"禁止ワード一覧:\n{text}", ephemeral=True)

# ──────────────────────────────────────────────
# on_voice_state_update (VC滞在時間の計測)
# ──────────────────────────────────────────────
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    guild_id = member.guild.id
    user_id = member.id
    uid_str = str(user_id)
    now = time.time()
    
    _vc_join_times.setdefault(guild_id, {})
    
    # 参加・移動
    if after.channel is not None:
        if user_id not in _vc_join_times[guild_id]:
            _vc_join_times[guild_id][user_id] = now
    
    # 退出・移動
    if before.channel is not None and (after.channel is None or before.channel != after.channel):
        if user_id in _vc_join_times[guild_id]:
            join_time = _vc_join_times[guild_id].pop(user_id)
            duration = now - join_time
            if duration > 0:
                _init_vc_ranking(guild_id)
                _vc_ranking_cache[guild_id].setdefault("vc_time", {})
                _vc_ranking_cache[guild_id]["vc_time"][uid_str] = _vc_ranking_cache[guild_id]["vc_time"].get(uid_str, 0.0) + duration
        
        if after.channel is not None:
            _vc_join_times[guild_id][user_id] = now

# ──────────────────────────────────────────────
# 互換モード用（FakeInteraction / ModalProxy）
# ──────────────────────────────────────────────
class ModalProxyView(discord.ui.View):
    def __init__(self, modal: discord.ui.Modal):
        super().__init__(timeout=300)
        self.modal = modal
        
    @discord.ui.button(label="入力画面を開く", style=discord.ButtonStyle.primary)
    async def btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self.modal)

class FakeResponse:
    def __init__(self, message):
        self.message = message
        self.is_done = False
    async def send_message(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        self.is_done = True
        return await self.message.channel.send(*args, **kwargs)
    async def defer(self, *args, **kwargs):
        self.is_done = True
        return
    async def send_modal(self, modal):
        self.is_done = True
        try:
            view = ModalProxyView(modal)
            await self.message.channel.send(
                f"{self.message.author.mention} 互換モード（~コマンド）では直接入力画面を表示できません。\n以下のボタンから入力画面を開いてください。",
                view=view, delete_after=120)
        except Exception:
            pass

class FakeFollowup:
    def __init__(self, message):
        self.message = message
    async def send(self, *args, **kwargs):
        kwargs.pop("ephemeral", None)
        return await self.message.channel.send(*args, **kwargs)

class FakeInteraction:
    def __init__(self, message):
        self.message = message
        self.user = message.author
        self.guild = message.guild
        self.channel = message.channel
        self.guild_id = message.guild.id if message.guild else None
        self.channel_id = message.channel.id
        self.response = FakeResponse(message)
        self.followup = FakeFollowup(message)
        self.client = bot
        self.type = discord.InteractionType.application_command
        
    async def original_response(self):
        return self.message

async def _process_fake_interaction(message):
    parts = message.content[1:].split()
    if not parts: return False
    cmd_name = parts[0]
    args_list = parts[1:]
    
    commands = bot.tree.get_commands()
    target_cmd = None
    for c in commands:
        if c.name == cmd_name:
            target_cmd = c
            break
            
    if not target_cmd:
        return False
        
    kwargs = {}
    try:
        # app_commands.Command parameters parsing
        params = getattr(target_cmd, "parameters", None)
        if params is None and hasattr(target_cmd, "_params"):
            params = list(target_cmd._params.values())
        if params is None:
            params = []
            
        for i, param in enumerate(params):
            if i < len(args_list):
                val = args_list[i]
                typ = param.type
                if typ == discord.AppCommandOptionType.integer:
                    val = int(val)
                elif typ == discord.AppCommandOptionType.boolean:
                    val = val.lower() in ("true", "1", "yes", "on", "y")
                elif typ == discord.AppCommandOptionType.user:
                    val_id = re.sub(r"\D", "", val)
                    if val_id.isdigit():
                        val = message.guild.get_member(int(val_id)) or message.guild.get_member_named(val)
                elif typ == discord.AppCommandOptionType.channel:
                    val_id = re.sub(r"\D", "", val)
                    if val_id.isdigit():
                        val = message.guild.get_channel(int(val_id))
                elif typ == discord.AppCommandOptionType.role:
                    val_id = re.sub(r"\D", "", val)
                    if val_id.isdigit():
                        val = message.guild.get_role(int(val_id))
                kwargs[param.name] = val
            elif not param.required:
                if param.default is not discord.utils.MISSING:
                    kwargs[param.name] = param.default
            else:
                await message.channel.send(f"引数 `{param.name}` が不足しています。")
                return True
                
        fake_i = FakeInteraction(message)
        await target_cmd.callback(fake_i, **kwargs)
    except Exception as e:
        await message.channel.send(f"互換モード実行エラー: {e}")
    return True

# ──────────────────────────────────────────────
# on_message (禁止ワード / 自動返信 / 川柳 / えっち / グローバルチャット)
# ──────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
        
    if message.content.startswith("~"):
        if await _process_fake_interaction(message):
            return
            
    # miq / miqc トリガー（返信メッセージへのメンション）
    msg_lower = message.content.lower()
    _miq_trigger = (
        message.reference and message.reference.message_id
        and bot.user in message.mentions
        and ("miq" in msg_lower)
    )
    if _miq_trigger:
        try:
            target_msg = await message.channel.fetch_message(message.reference.message_id)
            if target_msg.content:
                use_color = "miqc" in msg_lower
                avatar_bytes = b""
                try:
                    if target_msg.author.display_avatar:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(target_msg.author.display_avatar.url) as resp:
                                if resp.status == 200:
                                    avatar_bytes = await resp.read()
                except: pass
                img_file = await _make_quote_file(
                    target_msg.content, target_msg.author.display_name, avatar_bytes,
                    guild=message.guild, username=target_msg.author.name, color=use_color
                )
                await message.channel.send(file=img_file, reference=target_msg, mention_author=False)
                return
        except Exception:
            pass

    # quote画像への返信で color/c → カラー化, gray/g → グレー化
    _recolor_kw = msg_lower.strip()
    if (message.reference and message.reference.message_id
            and _recolor_kw in ("color", "c", "gray", "grey", "g")):
        try:
            ref_msg = await message.channel.fetch_message(message.reference.message_id)
            # botが送ったメッセージで添付画像がある場合
            if ref_msg.author == bot.user and ref_msg.attachments:
                # 元のquoteを再生成するために必要な情報をembedまたはファイル名から取れないので、
                # 代わりに ref_msg の参照先を辿って元の発言者を特定する
                src_msg = None
                if ref_msg.reference and ref_msg.reference.message_id:
                    try:
                        src_msg = await message.channel.fetch_message(ref_msg.reference.message_id)
                    except Exception:
                        pass
                if src_msg and src_msg.content:
                    use_color = _recolor_kw in ("color", "c")
                    avatar_bytes = b""
                    try:
                        if src_msg.author.display_avatar:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(src_msg.author.display_avatar.url) as resp:
                                    if resp.status == 200:
                                        avatar_bytes = await resp.read()
                    except: pass
                    img_file = await _make_quote_file(
                        src_msg.content, src_msg.author.display_name, avatar_bytes,
                        guild=message.guild, username=src_msg.author.name, color=use_color
                    )
                    await message.channel.send(file=img_file, reference=src_msg, mention_author=False)
                    return
        except Exception:
            pass

    await bot.process_commands(message)
    # DM受信時 → えっち語録をランダム返信
    if not message.guild:
        if message.content.strip():
            await message.channel.send(random.choice(LEWD_REPLIES))
        return
    # 禁止ワード (大文字小文字無視・単語境界考慮)
    content_lower = message.content.lower()
    wb_data = db_read("wordblock", guild_id=message.guild.id)
    for word in wb_data.get("words", []):
        w = word.lower()
        # 日本語はどこに含まれてもNGで、英字は単語境界を考慮
        if re.search(r'\b' + re.escape(w) + r'\b', content_lower) if w.isascii() else (w in content_lower):
            try:
                await message.delete()
                await message.channel.send(
                    f"{message.author.mention} 禁止ワードが含まれていたため削除しました。",
                    delete_after=5)
            except Exception:
                pass
            return


    # ローマ字翻訳 (デフォルトON、!で始まる場合は無視)
    rmj_data = db_read("romaji", guild_id=message.guild.id)
    if rmj_data.get("server", True) or message.channel.id in rmj_data.get("channels", []):
        text = message.content.strip()
        if not text.startswith("!") and re.match(r"^[a-zA-Z0-9\s.,!?'-]+$", text) and re.search(r"[a-zA-Z]", text):
            # GROQで翻訳
            api_key = get_groq_api_key(message.guild.id if message.guild else None)
            if api_key:
                translated = await _groq_translate_romaji(text, api_key)
                if translated:
                    await message.reply(f"[翻訳]: {translated}")

    # 自動返信 (クールダウン3秒・完全一致or部分一致を設定で選べる)
    if not _check_rate(f"autoreply:{message.channel.id}", cooldown_sec=3.0):
        pass  # クールダウン中はスキップ
    else:
        ar_data = db_read("autoreply", guild_id=message.guild.id)
        for trigger, rd in ar_data.get("replies", {}).items():
            match_mode = rd.get("match", "partial")
            matched = (message.content == trigger) if match_mode == "exact" else (trigger in message.content)
            if matched:
                if rd.get("emoji"):
                    try:
                        await message.add_reaction(rd["emoji"])
                    except Exception:
                        pass
                if rd.get("text"):
                    await message.reply(rd["text"])
                break

    # 川柳検出 (デフォルトON)
    hk_data = db_read("haiku", guild_id=message.guild.id)
    if hk_data.get("server", True) or message.channel.id in hk_data.get("channels", []):
        await check_haiku(message)

    # 熱盛検知 (デフォルトON)
    am_data = db_read("atsumori", guild_id=message.guild.id)
    if am_data.get("server", True) or message.channel.id in am_data.get("channels", []):
        await check_atsumori(message)

    # えっち検出
    lw_data = db_read("lewd", guild_id=message.guild.id)
    if lw_data.get("server") or message.channel.id in lw_data.get("channels", []):
        await check_lewd(message)

    # グローバルチャット
    await relay_global_message(message)

    # AIチャット乱入処理
    active_chats = getattr(bot, "_active_chats", {})
    if message.channel.id in active_chats and not message.webhook_id:
        session = active_chats[message.channel.id]
        session["history"].append({
            "name": message.author.display_name,
            "content": message.content
        })
        if len(session["history"]) > 20:
            session["history"].pop(0)



# ──────────────────────────────────────────────
# 8. Verify認証
# ──────────────────────────────────────────────
VERIFY_LEVELS = {1:"ボタンを押すだけ",2:"「同意する」と入力",3:"一桁の計算",4:"二桁の計算",
                 5:"4文字コード入力",6:"6文字コード入力",7:"8文字コード入力",
                 8:"10文字コード入力",9:"12文字コード入力",10:"16文字コード入力"}

async def _grant_role(interaction: discord.Interaction, role_id: int):
    role = interaction.guild.get_role(role_id)
    if role and role not in interaction.user.roles:
        await interaction.user.add_roles(role)
    await interaction.response.send_message("認証完了！ロールを付与しました。", ephemeral=True)

class VerifyButton(discord.ui.Button):
    def __init__(self, level: int, role_id: int):
        super().__init__(label="認証する", style=discord.ButtonStyle.success, custom_id=f"verify_{role_id}_{level}")
        self.level = level; self.role_id = role_id
    async def callback(self, interaction: discord.Interaction):
        lv = self.level
        if lv == 1:
            await _grant_role(interaction, self.role_id)
        elif lv == 2:
            await interaction.response.send_modal(AgreeModal(self.role_id))
        elif lv <= 4:
            a, b = random.randint(1, 9*(lv-1)+1), random.randint(1, 9)
            await interaction.response.send_modal(MathModal(self.role_id, f"{a} + {b} = ?", str(a+b)))
        else:
            length = {5:4,6:6,7:8,8:10,9:12,10:16}.get(lv, 6)
            await interaction.response.send_modal(CodeModal(self.role_id, gen_code(length)))

class VerifyView(discord.ui.View):
    def __init__(self, level: int, role_id: int):
        super().__init__(timeout=None)
        self.add_item(VerifyButton(level, role_id))

class AgreeModal(discord.ui.Modal, title="同意確認"):
    agree = discord.ui.TextInput(label="「同意する」と入力してください", placeholder="同意する")
    def __init__(self, role_id): super().__init__(); self.role_id = role_id
    async def on_submit(self, interaction):
        if self.agree.value.strip() in ("同意する","同意"):
            await _grant_role(interaction, self.role_id)
        else:
            await interaction.response.send_message("「同意する」と入力してください。", ephemeral=True)

class MathModal(discord.ui.Modal, title="計算問題"):
    answer_input = discord.ui.TextInput(label="答えを入力", placeholder="数字")





class CodeModal(discord.ui.Modal, title="コード入力"):
    code_input = discord.ui.TextInput(label="コードを入力")
    def __init__(self, role_id, code):
        super().__init__(); self.role_id = role_id; self.code = code
        self.code_input.label = f"コードを入力: {code}"
    async def on_submit(self, interaction):
        if self.code_input.value.strip() == self.code:
            await _grant_role(interaction, self.role_id)
        else:
            await interaction.response.send_message("コードが違います。", ephemeral=True)

@bot.tree.command(name="verify", description="認証パネルを作成します")
@app_commands.describe(level="保護レベル（1〜10）", role="認証成功時に付与するロール")
async def cmd_verify(interaction: discord.Interaction, level: int, role: discord.Role):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("サーバー管理権限が必要です。", ephemeral=True)
        return
    if not 1 <= level <= 10:
        await interaction.followup.send("レベルは1〜10で指定してください。", ephemeral=True)
        return
    embed = discord.Embed(title="認証パネル", description=f"レベル {level} / {VERIFY_LEVELS[level]}\nボタンを押して認証してください。", color=0xEB459E)
    await interaction.channel.send(embed=embed, view=VerifyView(level, role.id))
    await interaction.followup.send("認証パネルを作成しました。", ephemeral=True)

# ──────────────────────────────────────────────
# 9. 自動返信 /autoreply
# ──────────────────────────────────────────────
@bot.tree.command(name="autoreply", description="自動返信を設定します")
@app_commands.describe(action="add / remove / list", trigger="トリガーワード", reply="返信テキスト", emoji="リアクション絵文字（省略可）")
async def cmd_autoreply(interaction: discord.Interaction, action: str, trigger: str = None, reply: str = None, emoji: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("メッセージ管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("autoreply", guild_id=interaction.guild_id)
    autoreplies = gd.get("replies", {})
    if action == "add":
        if not trigger or not reply:
            await interaction.followup.send("トリガーと返信テキストを指定してください。", ephemeral=True)
            return
        autoreplies[trigger] = {"text": reply, "emoji": emoji or ""}
        gd["replies"] = autoreplies
        db_write("autoreply", gd, guild_id=interaction.guild_id)
        await interaction.followup.send(f"自動返信を追加: `{trigger}`", ephemeral=True)
    elif action == "remove":
        autoreplies.pop(trigger or "", None)
        gd["replies"] = autoreplies
        db_write("autoreply", gd, guild_id=interaction.guild_id)
        await interaction.followup.send(f"`{trigger}` の自動返信を削除しました。", ephemeral=True)
    elif action == "list":
        text = "\n".join(f"`{k}` → {v['text']}" for k, v in autoreplies.items()) or "なし"
        await interaction.followup.send(f"自動返信一覧:\n{text}", ephemeral=True)

# ──────────────────────────────────────────────
# 10. リアクション /reaction & Context Menus
# ──────────────────────────────────────────────
@bot.tree.context_menu(name="Make it a quote")
async def context_quote(interaction: discord.Interaction, message: discord.Message):
    await safe_defer(interaction)
    
    avatar_bytes = b""
    try:
        if message.author.display_avatar:
            async with aiohttp.ClientSession() as session:
                async with session.get(message.author.display_avatar.url) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
    except:
        pass

    try:
        file = await _make_quote_file(message.content or " ", message.author.display_name, avatar_bytes, guild=interaction.guild, username=message.author.name)
        await interaction.followup.send(file=file)
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)

@bot.tree.context_menu(name="Obama")
async def context_obama(interaction: discord.Interaction, message: discord.Message):
    await safe_defer(interaction, ephemeral=True)
    obama_guild = bot.get_guild(OBAMA_GUILD_ID)
    if not obama_guild:
        await interaction.followup.send("obama絵文字のサーバーにBotが参加していません。", ephemeral=True)
        return
    emojis = []
    e = discord.utils.get(obama_guild.emojis, name="obama")
    if e: emojis.append(e)
    for i in range(1, 25):
        e = discord.utils.get(obama_guild.emojis, name=f"obama{i}")
        if e: emojis.append(e)
    if not emojis:
        await interaction.followup.send("obama絵文字が見つかりませんでした。", ephemeral=True)
        return
    random.shuffle(emojis)
    try:
        for em in emojis[:20]:
            await message.add_reaction(em)
        await interaction.followup.send("Obama絵文字でリアクションしました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"エラー: {e}", ephemeral=True)

@bot.tree.command(name="reaction", description="指定メッセージIDにobama絵文字25個をランダムでつけます")
@app_commands.describe(message_id="対象のメッセージID")
async def cmd_reaction(interaction: discord.Interaction, message_id: str):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("メッセージ管理権限が必要です。", ephemeral=True)
        return
    obama_guild = bot.get_guild(OBAMA_GUILD_ID)
    if not obama_guild:
        await interaction.followup.send("obama絵文字のサーバーにBotが参加していません。", ephemeral=True)
        return
    emojis = []
    e = discord.utils.get(obama_guild.emojis, name="obama")
    if e: emojis.append(e)
    for i in range(1, 25):
        e = discord.utils.get(obama_guild.emojis, name=f"obama{i}")
        if e: emojis.append(e)
    if not emojis:
        await interaction.followup.send("obama絵文字が見つかりませんでした。", ephemeral=True)
        return
    random.shuffle(emojis)
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
    except Exception:
        await interaction.followup.send("メッセージが見つかりませんでした。", ephemeral=True)
        return
    for emoji in emojis[:25]:
        try:
            await msg.add_reaction(emoji)
            await asyncio.sleep(0.3)
        except Exception:
            pass
    await interaction.followup.send("obamaをつけました！", ephemeral=True)

# ──────────────────────────────────────────────
# /letterreact
# ──────────────────────────────────────────────
_LETTER_EMOJI_MAP: dict[str, str] = {
    **{chr(ord("a") + i): chr(0x1F1E6 + i) for i in range(26)},   # a-z → 🇦-🇿
    **{chr(ord("A") + i): chr(0x1F1E6 + i) for i in range(26)},   # A-Z → 🇦-🇿
    "0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣",
    "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣",
    "!": "❗", "?": "❓", "+": "➕", "-": "➖",
}

@bot.tree.command(name="letterreact", description="指定したメッセージに文字の絵文字をリアクションします")
@app_commands.describe(message_id="対象のメッセージID", text="リアクションする文字（英数字）")
async def cmd_letterreact(interaction: discord.Interaction, message_id: str, text: str):
    await safe_defer(interaction, ephemeral=True)

    try:
        msg = await interaction.channel.fetch_message(int(message_id))
    except Exception:
        await interaction.followup.send("メッセージが見つかりませんでした。IDとチャンネルを確認してください。", ephemeral=True)
        return

    emojis_to_add = []
    skipped = []
    seen = set()
    for ch in text:
        if ch == " ":
            continue
        emoji = _LETTER_EMOJI_MAP.get(ch)
        if emoji is None:
            skipped.append(repr(ch))
            continue
        # 大文字・小文字は同じ絵文字になるので小文字に正規化して重複チェック
        key = ch.lower()
        if key in seen:
            skipped.append(f"重複: {repr(ch)}")
            continue
        seen.add(key)
        emojis_to_add.append(emoji)

    if not emojis_to_add:
        await interaction.followup.send("リアクションできる文字がありませんでした。英数字を指定してください。", ephemeral=True)
        return

    failed = []
    for emoji in emojis_to_add:
        try:
            await msg.add_reaction(emoji)
            await asyncio.sleep(0.4)
        except Exception:
            failed.append(emoji)

    result_lines = [f"✅ `{text}` を絵文字でリアクションしました！"]
    if skipped:
        result_lines.append(f"⚠️ スキップした文字: {', '.join(skipped)}")
    if failed:
        result_lines.append(f"❌ 失敗: {', '.join(failed)}")
    await interaction.followup.send("\n".join(result_lines), ephemeral=True)



# ──────────────────────────────────────────────
# 11. 川柳検出
# ──────────────────────────────────────────────
KANJI_YOMI: dict[str, str] = {
    "日":"ひ","月":"つき","山":"やま","川":"かわ","花":"はな","風":"かぜ","雨":"あめ",
    "雪":"ゆき","空":"そら","海":"うみ","木":"き","春":"はる","夏":"なつ","秋":"あき",
    "冬":"ふゆ","人":"ひと","心":"こころ","夢":"ゆめ","時":"とき","道":"みち",
    "光":"ひかり","影":"かげ","声":"こえ","手":"て","目":"め","耳":"みみ",
    "水":"みず","火":"ひ","土":"つち","草":"くさ","鳥":"とり","星":"ほし",
    "夜":"よる","朝":"あさ","昼":"ひる","今":"いま","子":"こ","父":"ちち","母":"はは",
    "家":"いえ","町":"まち","村":"むら","友":"とも","愛":"あい","涙":"なみだ",
    "笑":"わら","泣":"な","走":"はし","飛":"と","咲":"さ","散":"ち","落":"お",
    "白":"しろ","黒":"くろ","赤":"あか","青":"あお","緑":"みどり","桜":"さくら",
    "梅":"うめ","竹":"たけ","松":"まつ","葉":"は","森":"もり","野":"の","池":"いけ",
    "波":"なみ","岩":"いわ","石":"いし","霧":"きり","雪":"ゆき","虹":"にじ",
    "香":"かお","命":"いのち","神":"かみ","静":"しず","深":"ふか","遠":"とお",
    "大":"おお","小":"ちい","長":"なが","新":"あたら","古":"ふる",
    # 動詞・形容詞系
    "見":"み","聞":"き","言":"い","思":"おも","知":"し","来":"く","行":"い",
    "出":"で","入":"はい","立":"た","起":"お","寝":"ね","食":"た","飲":"の",
    "書":"か","読":"よ","歩":"あゆ","走":"はし","泳":"およ","飛":"と",
    "降":"ふ","照":"て","吹":"ふ","流":"なが","咲":"さ","散":"ち","落":"お",
    "揺":"ゆ","輝":"かがや","静":"しず","深":"ふか","遠":"とお","近":"ちか",
    "高":"たか","低":"ひく","速":"はや","遅":"おそ","明":"あか","暗":"くら",
    "熱":"あつ","冷":"つめ","甘":"あま","苦":"にが","辛":"から","酸":"す",
    # 場所・自然
    "丘":"おか","谷":"たに","峰":"みね","崖":"がけ","浜":"はま","沖":"おき",
    "湖":"みずうみ","滝":"たき","泉":"いずみ","砂":"すな","土":"つち",
    "石":"いし","岩":"いわ","霧":"きり","霜":"しも","露":"つゆ","虹":"にじ",
    "雷":"かみなり","嵐":"あらし","霞":"かすみ","煙":"けむり","炎":"ほのお",
    # 季語・風物詩
    "花":"はな","桜":"さくら","梅":"うめ","菊":"きく","蓮":"はす",
    "竹":"たけ","松":"まつ","杉":"すぎ","橡":"とち","柳":"やなぎ",
    "蝶":"ちょう","蛍":"ほたる","蝉":"せみ","鈴虫":"すずむし",
    "鴨":"かも","雀":"すずめ","鶯":"うぐいす","燕":"つばめ","鷹":"たか",
    "蛙":"かえる","蛇":"へび","亀":"かめ","魚":"さかな","蟹":"かに",
    # 人・心・時間
    "命":"いのち","魂":"たましい","心":"こころ","夢":"ゆめ","愛":"あい",
    "恋":"こい","涙":"なみだ","笑":"わら","泣":"な","祈":"いの",
    "願":"ねが","誓":"ちか","忘":"わす","想":"おも","恋":"こい",
    "旅":"たび","別":"わか","逢":"あ","待":"ま","惜":"お",
    "昨":"きのう","今":"いま","明":"あす","朝":"あさ","昼":"ひる",
    "夕":"ゆう","夜":"よる","宵":"よい","暁":"あかつき","晩":"ばん",
    "春":"はる","夏":"なつ","秋":"あき","冬":"ふゆ","年":"とし",
    "月":"つき","日":"ひ","時":"とき","刻":"とき","瞬":"またた",
}

def kanji_to_yomi(text: str) -> str:
    result = []
    for ch in text:
        if ch in KANJI_YOMI:
            result.append(KANJI_YOMI[ch])
        elif "\u4e00" <= ch <= "\u9fff":
            result.append("ああ")  # 未知漢字は平均2モーラとして扱う
        else:
            result.append(ch)
    return "".join(result)

def count_mora(text: str) -> int:
    skip = set("ぁぃぅぇぉっゃゅょァィゥェォッャュョーｰ")
    count = 0
    for ch in kanji_to_yomi(text):
        if "\u3041" <= ch <= "\u3096" or "\u30A1" <= ch <= "\u30F6":
            if ch not in skip:
                count += 1
        elif ch.isascii() and ch.isalpha():
            count += 1
    return count

def _try_haiku_match(candidate: str) -> list[str] | None:
    """候補文字列から5-7-5 or 5-5-7パターンを探す（±1字余り許容）"""
    clean = re.sub(r"[\s　、。,.・/\n！!？?～~「」『』【】【】\(\)（）]", "", candidate)
    if len(clean) < 5:
        return None
    n     = len(clean)
    total = count_mora(clean)
    if not (13 <= total <= 21):
        return None
    # 5-7-5 または 5-5-7 を ±1 で探索
    for p1_target, p2_target, p3_target in [(5,7,5),(5,5,7)]:
        for i in range(2, n - 2):
            m1 = count_mora(clean[:i])
            if not (p1_target - 1 <= m1 <= p1_target + 1):
                continue
            for j in range(i + 2, n):
                m2 = count_mora(clean[i:j])
                if m2 > p2_target + 2:
                    break
                if p2_target - 1 <= m2 <= p2_target + 1:
                    m3 = count_mora(clean[j:])
                    if p3_target - 1 <= m3 <= p3_target + 1:
                        return [clean[:i], clean[i:j], clean[j:]]
    return None

def split_into_phrases(text: str) -> list[str] | None:
    """
    川柳/俳句の3フレーズを検出する。
    - 長いメッセージの中からも探せる（文章をスライディングウィンドウで検索）
    - 区切り文字で明示的に3分割されている場合を最優先
    - 5-7-5 と 5-5-7 どちらも検出
    - ±1字余り・字足らず許容
    """
    stripped = text.strip()
    if stripped.startswith("http"):
        return None
    if len(stripped) < 5:
        return None

    # 1) 区切り文字で3分割できる場合（最優先）
    parts = re.split(r"[\s　、。,.・/\n！!？?～~]+", stripped)
    parts = [p for p in parts if p.strip()]
    if len(parts) == 3:
        moras = [count_mora(p) for p in parts]
        targets = [(5,7,5),(5,5,7)]
        for p1t,p2t,p3t in targets:
            if (p1t-1 <= moras[0] <= p1t+1 and
                p2t-1 <= moras[1] <= p2t+1 and
                p3t-1 <= moras[2] <= p3t+1):
                return parts

    # 2) テキスト全体または文章中のウィンドウで探索
    # 句読点・改行で文を分割してから各文を検索
    sentences = re.split(r"[。\n！？!?]", stripped)
    for sent in sentences:
        result = _try_haiku_match(sent)
        if result:
            return result

    # 3) 元のテキスト全体でも試す（句読点なしの場合）
    return _try_haiku_match(stripped)

# フォントキャッシュ（パス検索を1回だけ行う）
_FONT_PATH_CACHE: str | None = None

def _find_font_path() -> str | None:
    """日本語対応フォントパスを動的に検索する"""
    global _FONT_PATH_CACHE
    if _FONT_PATH_CACHE is not None:
        return _FONT_PATH_CACHE

    import subprocess as _sp

    # 優先: Macのヒラギノ明朝（見た目が最良）
    mac_candidates = [
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
        "/System/Library/Fonts/ヒラギノ明朝 ProN W3.otf",
        "/System/Library/Fonts/Hiragino Mincho ProN.ttc",
        "/System/Library/Fonts/Supplemental/Hiragino Mincho ProN W3.otf",
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/Library/Fonts/ヒラギノ明朝 ProN W3.otf",
        "/Library/Fonts/HiraginoSerif.ttc",
    ]
    for p in mac_candidates:
        if os.path.exists(p):
            _FONT_PATH_CACHE = p
            return p

    # フォールバック: fc-list で日本語フォントを検索
    prefer_keywords = ["Serif", "Mincho", "serif", "mincho"]
    try:
        r = _sp.run(["fc-list", ":lang=ja", "--format=%{file}\n"],
                    capture_output=True, text=True, timeout=4)
        paths = [l.strip() for l in r.stdout.strip().split("\n") if l.strip()]
        # 明朝系を優先
        for kw in prefer_keywords:
            for p in paths:
                if kw in p and os.path.exists(p):
                    _FONT_PATH_CACHE = p
                    return p
        # それ以外でも何かあれば使う
        for p in paths:
            if os.path.exists(p):
                _FONT_PATH_CACHE = p
                return p
    except Exception:
        pass

    _FONT_PATH_CACHE = ""   # 見つからない
    return None

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """日本語フォントをロード。見つからなければPillowビルトインフォントを使用"""
    path = _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    # Pillow 10以降は load_default(size=N) でビットマップではなく
    # ベクタフォントが返るが日本語は表示できないことが多い
    return ImageFont.load_default(size=size)


# ──────────────────────────────────────────────
# 作文 /sakubun
# ──────────────────────────────────────────────
KINSOKU_CHARS = set("、。，．」』ぁぃぅぇぉっゃゅょァィゥェォッャュョ")
COLS_PER_PAGE = 20

def get_groq_api_key(guild_id: int = None) -> str:
    if guild_id:
        st = db_read("aichat_settings", guild_id=guild_id)
        if st.get("custom_api_key"):
            return st["custom_api_key"]
    return AICHAT_API_KEY or GROQ_API_KEY

async def _groq_generate_sakubun(theme: str, length: int, guild_id: int = None) -> str:
    api_key = get_groq_api_key(guild_id)
    if not api_key: return ""
    prompt = (
        f"あなたは小学生です。\n"
        f"テーマ「{theme}」について、{length}文字程度の作文を書いてください。\n\n"
        "【絶対ルール】\n"
        "1. タイトル、氏名、挨拶などは一切書かないでください。\n"
        "2. 本文のみを純粋なテキストで出力してください。\n"
        "3. 改行や段落分けを適度に行ってください。\n"
        "4. 自然な日本語（小学生〜中学生らしい文体）で書いてください。"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500,
                    "temperature": 0.6,
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
    except: pass
    return ""

def _split_into_cols(text: str, title: str, author: str, rows: int = 20) -> list[list[str]]:
    # Title: indent 3
    title_line = ["　"] * 3 + list(title)
    
    # Author: end at 1-2 chars from bottom. 
    auth_chars = list(author)
    space_count = max(0, rows - 2 - len(auth_chars))
    author_line = ["　"] * space_count + auth_chars
    
    lines: list[list[str]] = [title_line, author_line]
    
    # Body
    paragraphs = text.split("\n")
    cur: list[str] = []
    
    for para in paragraphs:
        para = para.strip()
        if not para:
            lines.append([]) # empty line
            continue
            
        # Indent if not a conversation
        if not para.startswith("「"):
            para = "　" + para
            
        i = 0
        while i < len(para):
            ch = para[i]
            if para[i:i+2] == "。」":
                ch = "。」"
                i += 1
            if len(cur) >= rows:
                if ch in KINSOKU_CHARS or ch == "。」":
                    cur.append(ch)
                else:
                    lines.append(cur)
                    cur = [ch]
            else:
                cur.append(ch)
            i += 1
            
    if cur:
        lines.append(cur)
    return lines

def _render_page(cols: list[list[str]], page: int, total: int, title: str) -> discord.File:
    ROWS = 20
    CELL = 36
    MARGIN = 55
    actual = max(COLS_PER_PAGE, len(cols))
    W = MARGIN * 2 + actual * CELL
    H = MARGIN * 2 + ROWS * CELL + 60
    img = Image.new("RGB", (W, H), "#fdfbf7")
    draw = ImageDraw.Draw(img)
    RED = "#b82c2c"

    font = ImageFont.load_default()
    small_font = font
    for fp in ["/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
               "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
               "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]:
        if os.path.exists(fp):
            font = ImageFont.truetype(fp, int(CELL * 0.8))
            small_font = ImageFont.truetype(fp, 16)
            break

    page_str = f"{title}  ({page}/{total}ページ)"
    draw.text((MARGIN, 10), page_str, fill="#555555", font=small_font)

    top = MARGIN + 50; bottom = H - MARGIN
    left = MARGIN; right = W - MARGIN
    draw.rectangle([left, top, right, bottom], outline=RED, width=2)
    for i in range(1, actual):
        x = left + i * CELL
        draw.line([x, top, x, bottom], fill=RED, width=1)
    for i in range(1, ROWS):
        y = top + i * CELL
        draw.line([left, y, right, y], fill=RED, width=1)

    for dy in range(-1, 2):
        ym = top + 10 * CELL + dy
        draw.line([left, ym, right, ym], fill="#fdfbf7", width=1)

    for ci, col_chars in enumerate(cols):
        x = right - (ci + 1) * CELL
        for ri, ch in enumerate(col_chars):
            y = top + ri * CELL
            if ch == "。」":
                cx1, cy1 = x + CELL * 0.6, y - CELL * 0.1
                cx2, cy2 = x + CELL * 0.1, y + CELL * 0.05
                draw.text((cx1, cy1), "。", fill="#1a1a1a", font=font)
                draw.text((cx2, cy2), "」", fill="#1a1a1a", font=font)
                continue

            cx, cy = x + CELL * 0.1, y + CELL * 0.05
            if ch in ["、", "。", "，", "．"]:
                cx, cy = x + CELL * 0.6, y - CELL * 0.1
            elif ch in ["っ","ゃ","ゅ","ょ","ぁ","ぃ","ぅ","ぇ","ぉ","ッ","ャ","ュ","ョ","ァ","ィ","ゥ","ェ","ォ"]:
                cx = x + CELL * 0.3
            
            draw.text((cx, cy), ch, fill="#1a1a1a", font=font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=f"sakubun_p{page}.png")

def build_sakubun_images(text: str, title: str, author: str) -> list[discord.File]:
    all_cols = _split_into_cols(text, title, author)
    pages_cols = [all_cols[i:i+COLS_PER_PAGE] for i in range(0, max(1, len(all_cols)), COLS_PER_PAGE)]
    total = len(pages_cols)
    return [_render_page(pc, p+1, total, title) for p, pc in enumerate(pages_cols)]

@bot.tree.command(name="sakubun", description="指定したテーマと文字数でAIが作文を書き、原稿用紙の画像として出力します")
@app_commands.describe(theme="作文のテーマ", length="文字数の目安（例: 400）", author="筆者名（省略可）")
async def cmd_sakubun(interaction: discord.Interaction, theme: str, length: int = 400, author: str = "名無し"):
    await safe_defer(interaction)
    if length > 1600:
        await interaction.followup.send("文字数は1600文字以内で指定してください。")
        return
    text = await _groq_generate_sakubun(theme, length)
    if not text:
        await interaction.followup.send("作文の生成に失敗しました。")
        return
    files = await asyncio.to_thread(build_sakubun_images, text, theme, author)
    for chunk in [files[i:i+10] for i in range(0, len(files), 10)]:
        await interaction.followup.send(files=chunk)

_VERTICAL_EMOJI_PATTERN = re.compile(
    r"(<a?:[^:>]+:\d+>)"
    r"|"
    r"([\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\U00002600-\U000027BF\U0001F1E0-\U0001F1FF]"
    r"(?:\uFE0F|\u200D[\U0001F300-\U0001F9FF])*)"
)

def _split_vertical_tokens(text: str) -> list:
    """縦書き描画用に文字列を1文字ずつ、絵文字はひとかたまりのトークンに分割する"""
    tokens = []
    last = 0
    for m in _VERTICAL_EMOJI_PATTERN.finditer(text):
        if m.start() > last:
            tokens.extend(list(text[last:m.start()]))
        tokens.append(m.group())
        last = m.end()
    if last < len(text):
        tokens.extend(list(text[last:]))
    return tokens

def build_haiku_image(parts: list[str], emoji_images: dict = None) -> Image.Image:
    """
    縦書き・和紙風俳句カード。W=380 H=560 固定。
    最長句の文字数でフォントサイズ・char_hを動的計算し枠内に必ず収める。
    絵文字（Unicode・カスタム）はダウンロード済み画像があれば画像として描画する。
    """
    import random as _rnd

    if emoji_images is None:
        emoji_images = {}

    W       = 380
    H       = 560
    PAD_X   = 48
    COL_GAP = 130
    TOP_Y   = 50
    BOT_PAD = 30

    BG_TOP    = (253, 250, 238)
    BG_BOT    = (245, 240, 215)
    INK       = (45, 30, 15)
    FRAME_OUT = (180, 148, 92)
    FRAME_IN  = (212, 186, 132)

    # 絵文字は1文字クラスタとして扱い、それを踏まえた最長句のトークン数でサイズを決定
    token_lists = [_split_vertical_tokens(p) for p in parts]
    max_len   = max((len(t) for t in token_lists), default=7) or 7
    avail_h   = H - TOP_Y - BOT_PAD        # 描画可能な縦幅 = 480
    char_h    = avail_h // max(max_len, 1)
    font_size = min(42, max(18, int(char_h * 0.80)))
    char_h    = max(char_h, font_size + 4)  # 文字間が詰まりすぎないように

    img  = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)

    # グラデ背景
    for yi in range(H):
        t = yi / H
        draw.line([(0, yi), (W, yi)], fill=(
            int(BG_TOP[0] + (BG_BOT[0]-BG_TOP[0]) * t),
            int(BG_TOP[1] + (BG_BOT[1]-BG_TOP[1]) * t),
            int(BG_TOP[2] + (BG_BOT[2]-BG_TOP[2]) * t),
        ))

    # 和紙ノイズ
    for _ in range(2000):
        xi = _rnd.randint(0, W-1); yi = _rnd.randint(0, H-1)
        v  = _rnd.randint(218, 250)
        draw.point((xi, yi), fill=(v, v-5, v-14))

    # 枠
    draw.rectangle([6, 6, W-7, H-7],     outline=FRAME_OUT, width=3)
    draw.rectangle([13, 13, W-14, H-14], outline=FRAME_IN,  width=1)
    for cx, cy in [(6,6),(W-7,6),(6,H-7),(W-7,H-7)]:
        d = 7
        draw.polygon([(cx,cy-d),(cx+d,cy),(cx,cy+d),(cx-d,cy)], fill=FRAME_OUT)

    f_main = _load_font(font_size)

    # 3列のX座標（右→中→左）
    col_xs = [W - PAD_X, W - PAD_X - COL_GAP, W - PAD_X - COL_GAP * 2]

    # 縦書き3列（絵文字はダウンロード済み画像があれば画像として貼り付け）
    for col_idx, tokens in enumerate(token_lists):
        cx = col_xs[col_idx]
        y  = TOP_Y
        for token in tokens:
            if y + font_size > H - BOT_PAD:  # 枠内に収まらなければ停止
                break
            em_data = emoji_images.get(token)
            if em_data:
                try:
                    em_img = Image.open(BytesIO(em_data)).convert("RGBA")
                    em_img = em_img.resize((font_size, font_size), Image.Resampling.LANCZOS)
                    img.paste(em_img, (cx - font_size // 2, y), mask=em_img)
                    y += char_h
                    continue
                except Exception:
                    pass
            draw.text((cx, y), token, font=f_main, fill=INK, anchor="mt")
            y += char_h

    return img


async def _groq_extract_haiku(text: str, guild_id: int = None) -> list[str] | None:
    """
    GROQを使って文章中の川柳を検出する。
    5-7-5に限定せず、字余り・字足らずも許容する。
    """
    api_key = get_groq_api_key(guild_id)
    if not api_key:
        return None
    try:
        prompt = (
            "あなたは川柳・俳句の専門家です。\n"
            "次の【元の文章】から、川柳として厳密に5・7・5（17モーラ）になっている部分があれば、それを抽出してください。\n"
            f"【元の文章】: 「{text}」\n\n"
            "【絶対ルール】\n"
            "- 抽出する句1・句2・句3は【元の文章】に連続して含まれる文字だけをそのまま使うこと。\n"
            "- 元の文章にない言葉を追加・変更・創作することは絶対に禁止。\n"
            "- 長い文章の一部だけが5-7-5になっている場合、その部分だけを抽出すること。\n"
            "- 上の句(5モーラ) / 中の句(7モーラ) / 下の句(5モーラ)に厳格に区切れるものだけを対象とする。\n"
            "- 字余り・字足らずは一切認めない。必ずピッタリ5・7・5のリズムになっているか発音（モーラ）で確認すること。\n\n"
            "川柳として厳密に5・7・5の部分が見つかった場合のみ、以下の形式だけで答えてください（説明不要）:\n"
            "句1|句2|句3\n\n"
            "例: 元「昨日食べたラーメン美味しかったけど古池や蛙飛び込む水の音でびっくりした」→ 古池や|蛙飛び込む|水の音\n\n"
            "厳密な5-7-5が見つからない場合、または字余り・字足らずの場合は「なし」とだけ答えてください。"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.1,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data   = await resp.json()
                result = data["choices"][0]["message"]["content"].strip()
                # "なし" または | がなければ非川柳
                if "なし" in result or "|" not in result:
                    return None
                # 最初の | 区切り行だけを使う（複数行あっても1件のみ）
                clean_text = re.sub(r"[\s\u3000\u3001\u3002,.!/?〜~]", "", text)
                for line in result.splitlines():
                    if "|" not in line:
                        continue
                    parts = [p.strip() for p in line.split("|")]
                    # 3フレーズ・各フレーズ非空・2〜10モーラ
                    if len(parts) != 3 or not all(p for p in parts):
                        break
                    if not all(2 <= count_mora(p) <= 10 for p in parts):
                        break
                    # 重要: 3フレーズを結合した文字列が元テキストに含まれるか検証
                    joined = re.sub(r"[\s\u3000\u3001\u3002,.!/?〜~]", "", "".join(parts))
                    if joined not in clean_text:
                        break   # 創作が含まれているのでNG
                    return parts
                return None
    except Exception:
        pass
    return None

# 川柳重複検知防止: 処理中のメッセージIDを記録
_haiku_processing: set[int] = set()

async def check_haiku(message: discord.Message):
    text = message.content.strip()
    if not text or len(text) < 5 or text.startswith("http"):
        return
    if text.startswith("/"):
        return
    # 同一メッセージに二重処理しない
    if message.id in _haiku_processing:
        return
    _haiku_processing.add(message.id)
    try:
        parts = None
        # GROQを優先（字余り・字足らず・文章中の検出が得意）
        api_key = get_groq_api_key(message.guild.id if message.guild else None)
        if api_key and 5 <= len(text) <= 120:
            parts = await _groq_extract_haiku(text)
        else:
            # GROQがない場合のみローカル検出
            parts = split_into_phrases(text)
        if parts:
            emoji_images = await _fetch_emoji_images("".join(parts), guild=message.guild)
            img = build_haiku_image(parts, emoji_images=emoji_images)
            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            await message.channel.send(
                "川柳を検出しました！",
                file=discord.File(buf, "senryu.png"),
                reference=message,
            )
    finally:
        _haiku_processing.discard(message.id)

@bot.tree.command(name="haiku", description="川柳検出機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF", channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_haiku(interaction: discord.Interaction, scope: str = "channel", state: str = "ON", channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("haiku", guild_id=interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["server"] = on
        msg = f"サーバー全体の川柳検出を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["channels"] = chs
        msg = f"{target.mention} の川柳検出を {'ON' if on else 'OFF'} にしました。"
    db_write("haiku", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(msg, ephemeral=True)


# ──────────────────────────────────────────────
# 12. リソースモニター
# ──────────────────────────────────────────────
def _bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)

def _color_from_pct(pct: float) -> int:
    return 0xED4245 if pct >= 85 else (0xFEE75C if pct >= 60 else 0x57F287)

async def build_resource_embed(client: discord.Client) -> discord.Embed:
    cpu    = psutil.cpu_percent(interval=0.5)
    mem    = psutil.virtual_memory()
    disk   = psutil.disk_usage("/")
    up_sec = int(time.time() - START_TIME)
    d, rem = divmod(up_sec, 86400); h, rem = divmod(rem, 3600); m, s = divmod(rem, 60)
    uptime = f"{d}d {h:02d}:{m:02d}:{s:02d}"
    lat    = round(client.latency * 1000, 1)
    db_size = 0
    if os.path.exists(DB_DIR):
        for dirpath, _, filenames in os.walk(DB_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    db_size += os.path.getsize(fp)
    dsz = db_size / 1024

    embed = discord.Embed(title="リソースモニター", color=_color_from_pct(max(cpu, mem.percent, disk.percent)),
                          timestamp=datetime.datetime.utcnow())
    embed.add_field(name="CPU",        value=f"`{_bar(cpu)}` {cpu:.1f}%", inline=False)
    embed.add_field(name="メモリ",      value=f"`{_bar(mem.percent)}` {mem.percent:.1f}%  ({mem.used//1024//1024:,}MB / {mem.total//1024//1024:,}MB)", inline=False)
    embed.add_field(name="ストレージ",  value=f"`{_bar(disk.percent)}` {disk.percent:.1f}%  ({disk.used//1024**3:.1f}GB / {disk.total//1024**3:.1f}GB)", inline=False)
    embed.add_field(name="アップタイム", value=uptime, inline=True)
    embed.add_field(name="Ping",        value=f"{lat} ms", inline=True)
    embed.add_field(name="db/",         value=f"{dsz:.1f} KB", inline=True)
    
    gl = getattr(client, "_groq_ratelimit", {})
    req_rem = gl.get("req_rem", "N/A")
    req_lim = gl.get("req_lim", "N/A")
    tok_rem = gl.get("tok_rem", "N/A")
    tok_lim = gl.get("tok_lim", "N/A")
    embed.add_field(name="Groq API", value=f"{req_rem} / {req_lim}", inline=True)
    embed.add_field(name="Groq API (Tokens)",   value=f"{tok_rem} / {tok_lim}", inline=True)
    return embed

@bot.tree.command(name="resource", description="サーバーのリソース状態を確認します")
async def cmd_resource(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    embed = await build_resource_embed(bot)
    await interaction.followup.send(embed=embed)

# ──────────────────────────────────────────────
# 13. バックアップと復元
# ──────────────────────────────────────────────
def _serialize_overwrites(overwrites: dict) -> dict:
    result = {}
    for target, overwrite in overwrites.items():
        key  = ("role_" if isinstance(target, discord.Role) else "member_") + str(target.id)
        allow, deny = overwrite.pair()
        result[key] = {"allow": allow.value, "deny": deny.value, "name": getattr(target, "name", "")}
    return result

def _deserialize_overwrites(guild: discord.Guild, overwrites_data: dict) -> dict:
    result = {}
    if not overwrites_data: return result
    for key, data in overwrites_data.items():
        if key.startswith("role_"):
            name = data.get("name")
            role = discord.utils.get(guild.roles, name=name)
            if not role and name == "@everyone":
                role = guild.default_role
            if role:
                result[role] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(data["allow"]),
                    discord.Permissions(data["deny"])
                )
    return result

async def _perform_save(interaction: discord.Interaction, guild: discord.Guild):
    backup = {
        "guild_name": guild.name,
        "saved_at":   datetime.datetime.now().isoformat(),
        "roles":      [],
        "categories": [],
        "channels":   [],
        "everyone_permissions": guild.default_role.permissions.value,
    }

    # ロール: 高位（position降順）から保存
    for role in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if role.is_bot_managed() or role.name == "@everyone":
            continue
        backup["roles"].append({
            "name":        role.name,
            "color":       role.color.value,
            "hoist":       role.hoist,
            "mentionable": role.mentionable,
            "permissions": role.permissions.value,
            "position":    role.position,
        })

    # カテゴリ（ポジション順）
    for cat in sorted(guild.categories, key=lambda c: c.position):
        backup["categories"].append({
            "name":       cat.name,
            "position":   cat.position,
            "overwrites": _serialize_overwrites(cat.overwrites),
        })

    # チャンネル（カテゴリ除外 + (カテゴリpos,チャンネルpos)でソート）
    for ch in sorted([c for c in guild.channels if not isinstance(c, discord.CategoryChannel)],
                     key=lambda c: (c.category.position if c.category else -1, c.position)):
        ow = ch.overwrites_for(guild.default_role)
        is_private = ow.view_channel is False
        ch_data = {
            "name":         ch.name,
            "type":         str(ch.type),
            "position":     ch.position,
            "cat_position": ch.category.position if ch.category else -1,
            "overwrites":   _serialize_overwrites(ch.overwrites),
            "category":     ch.category.name if ch.category else None,
            "nsfw":         getattr(ch, "nsfw", False),
            "topic":        getattr(ch, "topic", None),
            "slowmode":     getattr(ch, "slowmode_delay", 0),
            "private":      is_private,
            "news":         isinstance(ch, discord.TextChannel) and ch.is_news(),
        }
        backup["channels"].append(ch_data)
    code = gen_code(8)
    backup["code"] = code
    old_backup = db_read("backup", guild_id=guild.id)
    # 上書き前に古いコードを db/codes/index.json から削除
    if old_backup and isinstance(old_backup, dict) and old_backup.get("code"):
        codes = db_read("codes", shared="index")
        if isinstance(codes, dict):
            codes.pop(old_backup["code"], None)
            db_write("codes", codes, shared="index")
    
    db_write("backup", backup, guild_id=guild.id)
    codes = db_read("codes", shared="index")
    if not isinstance(codes, dict): codes = {}
    codes[code] = str(guild.id)
    db_write("codes", codes, shared="index")

    embed = discord.Embed(title="バックアップ完了", color=0x57F287)
    embed.add_field(name="保存日時",    value=backup["saved_at"], inline=False)
    embed.add_field(name="共有コード",  value=f"`{code}`", inline=False)
    embed.add_field(name="ロール数",    value=str(len(backup["roles"])), inline=True)
    embed.add_field(name="チャンネル数", value=str(len(backup["channels"])), inline=True)
    embed.set_footer(text="このコードを他のサーバーで /restore code: で使えます")
    await interaction.followup.send(embed=embed)

class SaveOverwriteView(discord.ui.View):
    def __init__(self, guild, interaction_orig):
        super().__init__(timeout=30)
        self.guild = guild
    @discord.ui.button(label="上書きする", style=discord.ButtonStyle.danger)
    async def overwrite(self, interaction, button):
        self.stop()
        await interaction.response.defer()
        await _perform_save(interaction, self.guild)
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)

@bot.tree.command(name="save", description="サーバーのロール・チャンネル・権限をバックアップします")
async def cmd_save(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("管理者権限が必要です。", ephemeral=True)
        return
    bk = db_read("backup", guild_id=interaction.guild_id)
    if bk and isinstance(bk, dict) and bk.get("saved_at"):
        ex = bk
        embed = discord.Embed(title="既存バックアップがあります",
            description=f"保存日時: **{ex.get('saved_at','不明')}**\nコード: `{ex.get('code','不明')}`\n\n上書きしますか？",
            color=0xFEE75C)
        await interaction.followup.send(embed=embed, view=SaveOverwriteView(interaction.guild, interaction))
        return
    await _perform_save(interaction, interaction.guild)

async def do_restore(interaction: discord.Interaction, backup: dict):
    guild = interaction.guild
    dm = None
    try:
        dm = await interaction.user.create_dm()
    except Exception:
        pass

    async def progress(txt: str):
        if dm:
            try: await dm.send(f"[復元中] {txt}")
            except Exception: pass

    await progress("チャンネルを削除中...")
    for ch in list(guild.channels):
        try: await ch.delete(); await asyncio.sleep(0.4)
        except Exception: pass

    await progress("ロールを削除中...")
    for role in list(guild.roles):
        if role.is_bot_managed() or role.name == "@everyone" or role >= guild.me.top_role:
            continue
        try: await role.delete(); await asyncio.sleep(0.4)
        except Exception: pass

    await progress("ロールを復元中...")
    # ロールをposition昇順（低位→高位）で作成する
    # Discordは create_role すると常に最下位に追加されるため、
    # 低位から順に作成することで積み上がって正しい順序になる
    # 高位(position大)→低位の順で作成
    # Discordは create_role すると最下位に追加されるため、
    # 高位から作成すれば後から作るものが下に積まれて正しい順序になる
    roles_sorted = sorted(backup.get("roles", []), key=lambda r: r["position"], reverse=True)
    for rd in roles_sorted:
        try:
            await guild.create_role(
                name=rd["name"],
                color=discord.Color(rd["color"]),
                hoist=rd["hoist"],
                mentionable=rd["mentionable"],
                permissions=discord.Permissions(rd["permissions"]),
            )
            await asyncio.sleep(0.35)
        except Exception as e:
            pass

    await progress("カテゴリを復元中...")
    cat_map = {}
    for cd in sorted(backup.get("categories", []), key=lambda c: c["position"]):
        try:
            ow = _deserialize_overwrites(guild, cd.get("overwrites", {}))
            cat = await guild.create_category(name=cd["name"], overwrites=ow)
            cat_map[cd["name"]] = cat
            await asyncio.sleep(0.3)
        except Exception:
            pass

    await progress("チャンネルを復元中...")
    log_ch = None
    for chd in sorted(backup.get("channels", []),
                      key=lambda c: (c.get("cat_position", 0), c.get("position", 0))):
        try:
            cat = cat_map.get(chd.get("category"))
            ct  = chd["type"]
            ow = _deserialize_overwrites(guild, chd.get("overwrites", {}))
            if "text" in ct or "news" in ct:
                new_ch = await guild.create_text_channel(
                    name=chd["name"], category=cat, overwrites=ow,
                    nsfw=chd.get("nsfw", False), topic=chd.get("topic"),
                    slowmode_delay=chd.get("slowmode", 0))
                if "news" in ct:
                    try: await new_ch.edit(type=discord.ChannelType.news)
                    except: pass
                if chd.get("private"):
                    await new_ch.set_permissions(guild.default_role, view_channel=False)
                if log_ch is None and not chd.get("private"):
                    log_ch = new_ch
            elif "voice" in ct:
                nv = await guild.create_voice_channel(name=chd["name"], category=cat, overwrites=ow)
                if chd.get("private"):
                    await nv.set_permissions(guild.default_role, view_channel=False)
            elif "stage" in ct:
                await guild.create_stage_channel(name=chd["name"], category=cat, overwrites=ow)
            elif "forum" in ct:
                await guild.create_forum(name=chd["name"], category=cat, overwrites=ow)
            await asyncio.sleep(0.3)
        except Exception:
            pass

    ep = backup.get("everyone_permissions")
    if ep is not None:
        try: await guild.default_role.edit(permissions=discord.Permissions(ep))
        except Exception: pass

    await progress("復元完了！")
    if log_ch:
        try: await log_ch.send("サーバーの復元が完了しました。")
        except Exception: pass

class RestoreConfirmView(discord.ui.View):
    def __init__(self, backup, interaction_orig):
        super().__init__(timeout=30)
        self.backup = backup
    @discord.ui.button(label="復元する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.stop(); await interaction.response.defer()
        await do_restore(interaction, self.backup)
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)

@bot.tree.command(name="restore", description="バックアップからサーバーを復元します")
@app_commands.describe(code="他サーバーのコード（省略=自サーバー의バックアップ）")
async def cmd_restore(interaction: discord.Interaction, code: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("管理者権限が必要です。", ephemeral=True)
        return
    backup = None
    if code:
        codes = db_read("codes", shared="index")
        sgid = codes.get(code) if isinstance(codes, dict) else None
        if not sgid:
            await interaction.followup.send("コードが見つかりません。", ephemeral=True)
            return
        backup = db_read("backup", guild_id=int(sgid))
        if not backup or not isinstance(backup, dict) or not backup.get("saved_at"):
            await interaction.followup.send("バックアップが存在しません。", ephemeral=True)
            return
    else:
        backup = db_read("backup", guild_id=interaction.guild_id)
        if not backup or not isinstance(backup, dict) or not backup.get("saved_at"):
            await interaction.followup.send("バックアップがありません。/save で作成してください。", ephemeral=True)
            return
    embed = discord.Embed(title="復元の確認",
        description=f"日時: **{backup.get('saved_at','不明')}**\n\n**現在のチャンネル・ロールはすべて削除されます。**\n本当に復元しますか？",
        color=0xED4245)
    await interaction.followup.send(embed=embed, view=RestoreConfirmView(backup, interaction))


# ──────────────────────────────────────────────
# 14. えっち検出
# ──────────────────────────────────────────────
LEWD_KEYWORDS = [
    "えっち","ecchi","ふたなり","おっぱい","まんこ","ちんこ","セックス","sex",
    "抜いた","射精","オナ","エロ","ero","ぬいた","あんあん","おしり","パンツ",
    "下着","ブラ","ちくび","乳首","フェラ","手マン","潮吹き","イった","イく",
    "いかせて","おかず","興奮","ムラムラ","発情","やらしい","淫乱",
]

LEWD_REPLIES = [
    "あっ…そこ…ふかく…いれないで…おなかがへん…になっちゃう…///",
    "んぁっ…おまんこ…きもちよく…されたら…もうだめ…らめぇぇ…♡",
    "はぁ…はぁ…なかで…どくどく…してる…わかる…？…きもちぃ…♡♡",
    "や…やだっ…こんな…おくまで…とどいたら…いっちゃうぅ…///",
    "くちゅくちゅ…っていってる…はずかしいから…きかないでぇ…",
    "んっ…もうぬれぬれ…なの…はやくいれてほしくて…たまんない…♡",
    "あっあっ…おねがい…もっとはやく…ぐりぐりして…おねがいっ…♡",
    "ふぁ…クリトリス…そんな…ちゅーしたら…こわれちゃうぅ…///",
    "ん…ちくびがびんびん…になってるの…みないでぇ…きもちぃから…",
    "あぁ…おなか…ぽこぽこしてる…あなたのが…こんなに…おおきくて…♡",
    "だめ…やだ…でも…いかせてぇ…もう…がまんできない…いかせてぇぇ…",
    "んぁっ…ゆびで…なかを…かきまわしたら…だびょーって…でちゃうぅ…",
    "もうぐちょぐちょ…なの…きいてる？…ここ…ずっとうごかしてて…♡",
    "あっ…いっちゃう…ほんとに…いっちゃうから…とめないでぇぇ…",
    "はぁ…やばい…しりを…たたかれるの…なんで…こんなにきもちぃの…///",
    "んっ…おっぱい…もみながら…したいの？…変態…でもきもちぃ…♡",
    "ふぁぁ…ぜんぶ…のみこんじゃった…おなかいっぱいぃ…よかった…♡",
    "やだっ…いきなり…うしろも…さわらないで…ぁでも…きもちよかった…",
    "あっ…ぜんぶ…きもちぃ…くりも…なかも…しりも…ぜんぶぅ…♡♡",
    "んんっ…せーえき…いっぱいでてる…あったかくて…きもちぃ…♡",
    "あぁ…おまんこが…ひくひくしてる…のわかる？…まだいけそう…",
    "ふぁ…あんな…おっきいの…いれたのに…もっとほしいなんて…わたしへん？…///",
    "んもぅ…ぜんぶしらない…きもちよすぎて…あたまがとける…らめ…♡",
    "あっあっあっ…いく…いく…ほんとにいくぅぅ…とめないでぇぇ…",
    "はぁ…はぁ…なかで…びゅーって…されたら…また…いっちゃった…♡",
    "やっ…れろれろ…しながら…ゆびまで…いれないでぇ…きもちよすぎぃ…",
    "んっ…ふとももに…こすりつけてるの…わかってるから…ちゃんといれてぇ…♡",
    "ぁああ…しぼりとられてる…きもちぃ…もっとほしい…もっとぉ…♡♡",
    "やだ…くちで…してほしいの…おまんこ…なめてほしいの…おねがい…",
    "んあっ…いっしょに…いこ？…なかに…だしていいから…いっしょにぃ…♡",
    # ── 記号・崩し・余白を活かした強化版語録 ─────────────
    "ふぁっ…きもちぃよぉ…こんなの…しらなかった…まじで…やばいってぇ…///",
    "あっ…おまんこ…ひろがってる…かんじする…もっとおしこんでぇ…♡",
    "んっ…クリいじりながら…おくまでついたら…らめぇぇぇ…こわれる…///",
    "はぁっ…ぬれすぎて…じゅぽじゅぽおとしてる…はずかしいぃ…でもきもちぃ…♡",
    "あぁ…せなかからだかれながら…うごかれたら…なきそう…きもちぃ…♡♡",
    "んぁ…ちくびをこりこりしながら…したでなめられたら…いきかけた…///",
    "やぁっ…うしろにいれながら…クリもさわったら…もうだめぇぇ…♡",
    "ふっ…ふっ…おなかのなかみちみちで…くるしい…でもきもちよすぎてぇ…",
    "んんっ…はげしくうごかないで…おねがい…すぐいっちゃうから…でもきもちぃ…♡",
    "あっ…あったかいなかに…いっぱいだしてくれたら…うれしいぃ…♡♡",
    "ふぁ…クリをくちでチュッチュしながら…ゆびをいれないでぇ…いきすぎるぅ…///",
    "んっ…のみこめてるかな…ちゃんとぜんぶのみこみたい…おいしいぃ…♡",
    "あぁん…ぎゅってされながらうごかれると…こころもとけちゃいそう…♡♡♡",
    "はぁっ…おまんこがほしくておねだりしてるの…わかる？…いれてぇ…///",
    "んあっ…ゆっくりゆっくりやったらズルいよぉ…もっとはやくしてぇ…♡",
    "ふぁっ…しおがでちゃう…でちゃう…とめられない…やばい…いっちゃうぅ…",
    "あっ…えっちなかおしてるっていわないでぇ…じぶんでわかってるからぁ…///",
    "んっ…うごくたびにくちゅくちゅおとがして…はずかしすぎてりかんする…",
    "やっ…そんなにみつめながらしないでぇ…はずかしくていっちゃうぅ…///",
    "ふぁ…とろとろになってきた…もうじぶんがわかんない…♡",
    "んぁあ…おしりのあなもさわらないでぇ…そこまだだいじょうぶじゃないから…",
    "あっあっ…はやすぎてついていけない…でもきもちぃからやめないで…♡",
    "はぁ…せんせい…もっとおしえて…えっちなきもちよさを…もっとぉ…♡♡",
    "んっ…なかがぎゅってしてるわかる？…はなしたくなくてぎゅってしてる…♡",
    "ふぁぁ…なんかいでもいかせてほしいぃ…♡♡♡",
    "あっ…おっぱいたぷたぷゆれてるのみてるんでしょ…変態…♡",
    "んんっ…ふかいとことんとんされたら…なきながらいっちゃう…",
    "ふぁ…おまんこのなかぜんぶみせてあげる…もっとみてぇ…///",
    "あぁっ…くちでしごかれながらみあげたらめがあって…いきそう…♡",
    "んあっ…こんなおとでちゃってる…なかがびしょびしょだから…///",
    "はあっ…3かいめなのにまだきもちぃ…おかしくなってきた…♡",
    "ふぁっ…うごくたびになかをかきまわされて…いまここいちばんきもちぃ…♡♡",
    "んっ…おしっこもれそうじゃなくて…しおがでちゃいそうなの…",
    "あっ…えっちなおとさせながらなかにいれてほしい…♡",
    "んぁ…うしろからだきしめながらここをくりくりしたら…ずるいよぉ…♡",
    "ふぁっ…んんっ…いきそういきそういきそう…とめないでぇぇ…",
    "あっ…おへそのしたがじーんってしてる…もうすぐいけそう…♡",
    "はぁ…いいこいいこってあたまなでながらしたら…だいすきになっちゃう…♡♡",
    "んっ…ふかすぎておなかまでとどいてるきがする…こわれちゃう…でもきもちぃ…",
    "あっあっ…んんっ…いくいくいくぅぅぅ…あぁぁぁぁぁっ…♡♡♡",
    "やぁっ…せなかにしながらちくびをつねったら…ほんとにやばい…///",
    "んっ…きもちよくてなみだでてきた…なんでこんなにきもちぃの…♡",
    "ふぁぁ…なかにいっぱいだして…ぽたぽたたれてる…えっちだねわたし…///",
    "あっ…またかたくなってる…まだするの？…うれしいぃ…♡",
    "はぁっ…もうじゅんびできてるから…はやくいれてぇ…おねだりしてる…♡♡",
    "んっ…きもちよすぎてことばがでない…ただただあっあっあっ…",
    "ふぁ…おまんこだけじゃなくてくちもおしりも…ぜんぶつかっていいよ…♡",
    "あぁ…もうぐちゃぐちゃなのにやめてくれない…もっとめちゃくちゃにして…",
    "んっ…きょうなんかいいかせてくれるの…もうかぞえるきりょくもない…///",
    "ふぁ…かれしでもないのに…こんなにいかされたら…すきになっちゃうよ…♡",
    "あぁっ…みてて…わたしここがいちばんきもちぃから…ずっとここして…♡♡",
    "んんんっ…さいごにいっぱいなかにだして…おわりにして…おねがい…♡",
    # ── さらに追加・強化語録 ──────────────────────────────────
    "あっ…ここ…きもちよすぎて…あしがたたない…もうたおれそう…///",
    "んぁ…ずっとここで…うごかしてて…きもちよくてめがきえる…♡",
    "ふぁっ…さすったら…すぐぬれちゃった…じぶんでもびっくり…///",
    "はぁ…おなかのそこが…きゅんきゅんしてる…はやくちょうだい…♡♡",
    "んっ…ぬれてるとこ…みないでぇ…でもみてほしいきもちもある…///",
    "あぁ…ここに…すぽっていれたら…ぴったりはまる…きもちぃ…♡",
    "やっ…ずっといったりきたり…されたら…なんかでてきた…やばい…",
    "んんっ…にほんごわすれた…きもちよすぎて…あっあっしかいえない…///",
    "ふぁっ…したから…くりくりされながら…ふかくつかれたら…らめらめ…♡",
    "あっ…そこ…とくべつにきもちよいとこ…よくわかったね…すごい…♡♡",
    "はぁっ…おしりをもちあげさせて…さらにおくまでいれないでぇ…こわれる…///",
    "んぁっ…くちゅくちゅ…じゅぽじゅぽ…えっちなおとしかしてない…///",
    "ふぁ…うごくたびに…ちくびがゆれて…じぶんでもきもちよくなってる…♡",
    "あぁっ…さわられるまえから…もうびちょびちょだった…ごめんなさい…///",
    "んっ…かおにかかったぁ…はずかしい…でもきもちよかったよ…♡",
    "やぁ…なかでおっきくなるの…わかる…すごくきもちぃ…♡♡",
    "ふぁっ…じぶんのこえが…えっちすぎて…こわくなってきた…でもとまれない…",
    "あっ…きもちよくて…あしがぷるぷるしてる…もうたてない…///",
    "んぁ…ゆびいれたまま…くりくりしたら…いちびょうでいった…♡",
    "はぁ…おまんこが…すっごいひくひくしてる…まだほしいってしてる…♡♡",
    "ふぁぁっ…はだかでだかれながら…キスしてほしい…すきなひとに…♡",
    "んんっ…いったのに…まだうごかしてるの…きもちよすぎておかしくなる…",
    "あっ…ちんちんのかたち…なかでかんじる…ここまでとどいてる…♡",
    "やぁ…だいすきなひとにいかされたら…ないてしまった…きもちぃ…♡♡",
    "ふぁ…ここをなめながら…ゆびをいれると…ちがうとこがいきそう…///",
    "んぁっ…かれし…いやキミ…ちょっとまって…いきそうだから…ちょっとまって…♡",
    "はぁっ…うえにのりながら…うんどうするの…はずかしいけどきもちぃ…///",
    "あっ…こんなにぬれてるのに…まだいじわる…いれてくれないの…♡",
    "んっ…ふとももをつかんで…はげしくされると…なかがきゅってする…♡♡",
    "ふぁぁ…3かいいかせてくれたら…なんでもします…だからもっとして…///",
    "やぁっ…うしろからあたまをおさえて…されると…ほんとにやばい…",
    "あぁ…ひとさしゆびと…なかゆびで…いっぺんにいれないでぇ…ひろがっちゃう…",
    "んんっ…せなかをなでながら…されると…なんかかなしくなってなける…きもちぃから…♡",
    "ふぁっ…いちどにいっぱいきもちよくなると…あたまがまっしろになる…///",
    "あっ…くちと…なかと…ゆびで…さんかしょいっぺんにされたら…もう…あぁ…♡♡",
    "んぁ…おしりのあな…ゆっくりほぐされてきた…こわいけどきもちぃ…",
    "はぁ…ずっとキスしながら…うごいてほしい…かおをみてほしい…♡",
    "ふぁっ…なかにはいってるの…わかる？…すごくきもちいいの…♡♡",
    "あぁっ…おかあさんになれるとこ…つっついたら…らめぇぇぇぇ…///",
    "んっ…えっちなことしながら…すきってきかれたら…こたえられない…♡",
    "やぁ…もうじぶんがだれかわかんない…きもちよすぎて…とけてる…♡",
    "ふぁぁ…ちゅっちゅしながら…したで…ころころされたら…いった…♡♡",
    "あっ…きょうはなんかいいかせてくれるの？…もうかぞえてない…///",
    "んんっ…このまま…あさになるまで…してほしい…♡",
    "はぁ…ぎゅってだきながら…なかにだしてくれたら…うれしくてなく…♡♡",
    "ふぁっ…もうここ…キミのかたちになってるかも…きもちぃ…♡",
    "あぁ…えっちなことしてる…わたし…でも…やめたくない…///",
    "んっ…いっぱいいかせてくれて…ありがとう…だいすき…♡♡♡",
    "ふぁぁぁ…らめぇ…もうらめぇ…でも…やめないでぇぇぇ…",
    "あっあっあっあぁっ…いっちゃう…いっちゃう…いくぅぅぅぅっ！！♡♡♡",
    # ── 記号テクニック全盛り・新語録 ──────────────────────────
    "ん…///…ここ…さわられるだけで…もうとろとろ…きもちぃ…♡",
    "あっ♡…やだっ♡…でもっ♡…やめないでっ♡…おねがいっ…♡♡",
    "はぁ…///…きもちよすぎて…わたし…おかしくなってるかも…///",
    "んっ…すき…すき…こんなにきもちよくして…すきになっちゃうじゃん…♡♡",
    "ふぁっ…ぁっ…んっ…もうことばにならない…あっあっあっ…///♡",
    "あぁ…まって…まって…いきそうだから…まってっ…♡///♡",
    "んぁ…ここ…ここっ…ずっとここ…はなれないで…♡…おねがい…♡",
    "はぁ♡…もっと…もっとぉ…もっとしてぇ…♡♡♡…たりないよぉ…",
    "ふぁ…///…みないでぇ…でも…みてほしい…♡…へんなの…わたし…///",
    "あっ…なかでぴくぴくしてる…わかる？…きもちよくてとまれない…♡",
    "んっ…こんなにぬらして…どうするの…♡…もうぐちゃぐちゃだよ…///",
    "やっ…ぁやっ…らめっ…らめぇ…でもきもちぃから…やめたくない…♡♡",
    "ふぁっ…キスしながら…ゆびいれないでぇ…あたまがとんじゃう…♡",
    "はぁ…もうなきそう…きもちよすぎて…うれしくてなきそう…♡♡",
    "んぁっ…すきすきすき…こんなきもちよくするひと…すきになっちゃうよ…♡♡♡",
]

async def _groq_check_lewd(text: str, guild_id: int = None) -> bool:
    api_key = get_groq_api_key(guild_id)
    if not api_key:
        return any(kw.lower() in text.lower() for kw in LEWD_KEYWORDS)
    try:
        prompt = (
            "あなたはDiscordサーバーの不適切な発言を監視するモデレーターです。\n"
            "次のメッセージが猥褻な表現を含むか判定してください。\n\n"
            f"メッセージ: 「{text}」\n\n"
            "「はい」にする条件（すべて当てはまる場合のみ）:\n"
            "- 読んだ誰もが見ても性的・猥褻とすぐわかる明白な表現である\n"
            "- 性的行為の描写、卑猥な隠語の性的使用、喘ぎ声など\n\n"
            "「いいえ」にする条件:\n"
            "- 曖昧・比喩・ジョーク・誤変換・日常語の偶然の一致\n"
            "- 少しでも迷いがある場合は必ず「いいえ」\n\n"
            "判定結果として「はい」または「いいえ」とだけ答えてください。"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.1,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data["choices"][0]["message"]["content"].strip()
                    return "はい" in res
    except Exception:
        pass
    return any(kw.lower() in text.lower() for kw in LEWD_KEYWORDS)

async def check_lewd(message: discord.Message):
    text = message.content.strip()
    if not text:
        return
    is_lewd = await _groq_check_lewd(text)
    if is_lewd:
        reply_text = random.choice(LEWD_REPLIES)
        # h_flan.png をアイコンにしたWebhookで送信
        try:
            hooks = await message.channel.webhooks()
            wh = next((h for h in hooks if h.name == "ｴｯﾁﾅﾌﾗﾝﾁｬﾝ"), None)
            if wh is None:
                # アイコン画像を読み込んでWebhookを作成
                img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "h_flan.png")
                if os.path.exists(img_path):
                    with open(img_path, "rb") as f:
                        avatar = f.read()
                    wh = await message.channel.create_webhook(name="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ", avatar=avatar)
                else:
                    wh = await message.channel.create_webhook(name="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ")
            await wh.send(reply_text, username="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ")
        except Exception:
            # Webhookが使えない場合は通常返信にフォールバック
            await message.channel.send(reply_text)

@bot.tree.command(name="lewd", description="えっち検出機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF", channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_lewd(interaction: discord.Interaction, scope: str = "channel", state: str = "ON", channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("lewd", guild_id=interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["server"] = on
        msg = f"サーバー全体のえっち検出を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["channels"] = chs
        msg = f"{target.mention} のえっち検出を {'ON' if on else 'OFF'} にしました。"
    db_write("lewd", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# 15. 画像取得 /h (NSFWチャンネル限定)
# ──────────────────────────────────────────────
RICK_GIFS = [
    "http://mamechosu.cloudfree.jp/dc/5655/cdn/gif/rick.gif",
    "http://mamechosu.cloudfree.jp/dc/5655/cdn/gif/rick1.gif",
]
# グロ・残虐系タグ除外リスト
_GURO_TAGS = [
    "guro", "gore", "blood", "amputee", "ryona", "vore",
    "scat", "torture", "death", "decapitation", "wound",
    "bruise", "injury", "cannibal",
]

async def fetch_yande_pool(session: aiohttp.ClientSession) -> list:
    """
    yande.re からランダムなNSFW画像をプールして返す。
    sample_url（プレビューサイズ）を優先取得。グロ系は除外。
    """
    urls = []
    seen = set()
    attempts = 0
    while len(urls) < 20 and attempts < 6:
        attempts += 1
        page = random.randint(1, 200)
        api_url = f"https://yande.re/post.json?tags=rating%3Aexplicit&limit=40&page={page}"
        try:
            async with session.get(
                api_url,
                headers={"User-Agent": "mamechosu-bot/1.0"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    continue
                posts = await resp.json()
                if not isinstance(posts, list):
                    continue
                for p in posts:
                    tag_str = p.get("tags", "").lower()
                    if any(g in tag_str for g in _GURO_TAGS):
                        continue
                    # sample_url (プレビュー) を優先、なければ file_url
                    su = p.get("sample_url") or p.get("file_url", "")
                    ext = su.rsplit(".", 1)[-1].lower() if su else ""
                    if su and ext in ("jpg", "jpeg", "png", "gif", "webp") and su not in seen:
                        urls.append(su)
                        seen.add(su)
        except Exception:
            continue
    return urls

_rick_streaks = {}

@bot.tree.command(name="h", description="えっちな画像をランダムで取得します")
@app_commands.guild_only()
async def cmd_h(interaction: discord.Interaction):
    ch = interaction.channel
    if not (isinstance(ch, discord.TextChannel) and ch.nsfw):
        await interaction.response.send_message("このコマンドはNSFW（年齢制限）チャンネルでのみ使用できます。", ephemeral=True)
        return
    await safe_defer(interaction)
    user_id = interaction.user.id
    if random.random() < 0.8:
        _rick_streaks[user_id] = 0
        async with aiohttp.ClientSession() as dl_session:
            pool = await fetch_yande_pool(dl_session)
            if not pool:
                await interaction.followup.send("画像の取得に失敗しました。")
                return
            # 最大3回リトライ
            random.shuffle(pool)
            sent = False
            for url in pool[:3]:
                try:
                    async with dl_session.get(
                        url,
                        headers={"User-Agent": "mamechosu-bot/1.0"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        raw = await resp.read()
                    ext = url.rsplit(".", 1)[-1].lower() if "." in url else "jpg"
                    fname = f"image.{ext}"
                    import io as _io
                    await interaction.followup.send(file=discord.File(_io.BytesIO(raw), filename=fname))
                    sent = True
                    break
                except Exception:
                    continue
            if not sent:
                await interaction.followup.send("画像の取得に失敗しました。")
    else:
        _rick_streaks[user_id] = _rick_streaks.get(user_id, 0) + 1
        rick_url = random.choice(RICK_GIFS)
        try:
            async with aiohttp.ClientSession() as rs:
                async with rs.get(rick_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        import io as _io
                        await interaction.followup.send(file=discord.File(_io.BytesIO(raw), filename="rick.gif"))
                    else:
                        await interaction.followup.send(rick_url)
        except Exception:
            await interaction.followup.send(rick_url)
        
        if _rick_streaks[user_id] >= 3:
            await interaction.channel.send(f"**なんと！！**\n{interaction.user.mention} さんが **3回連続でRickrollを引き当てました！** \nおめでとうございます！")
            _rick_streaks[user_id] = 0


# ──────────────────────────────────────────────
# /stats - サーバー活動統計画像
# ──────────────────────────────────────────────
@bot.tree.command(name="stats", description="サーバーの活動統計を画像で表示します")
@app_commands.describe(days="集計日数（1〜30、デフォルト7）")
async def cmd_stats(interaction: discord.Interaction, days: int = 7):
    await safe_defer(interaction)
    if not 1 <= days <= 30:
        await interaction.followup.send("1〜30日の範囲で指定してください。", ephemeral=True)
        return

    guild = interaction.guild
    now   = datetime.datetime.now(datetime.timezone.utc)

    # ── メンバー統計 ──────────────────────────────────
    total_members  = guild.member_count
    bot_members    = sum(1 for m in guild.members if m.bot)
    human_members  = total_members - bot_members
    online_members = sum(1 for m in guild.members
                         if m.status != discord.Status.offline and not m.bot)
    idle_members   = sum(1 for m in guild.members if m.status == discord.Status.idle and not m.bot)
    dnd_members    = sum(1 for m in guild.members if m.status == discord.Status.dnd  and not m.bot)

    # ── チャンネル統計 ────────────────────────────────
    text_chs   = len(guild.text_channels)
    voice_chs  = len(guild.voice_channels)
    categories = len(guild.categories)
    forum_chs  = len([c for c in guild.channels if isinstance(c, discord.ForumChannel)])
    stage_chs  = len([c for c in guild.channels if isinstance(c, discord.StageChannel)])

    # ── VC利用状況 ────────────────────────────────────
    vc_users = sum(len(vc.members) for vc in guild.voice_channels if vc.members)

    # ── メッセージ数を各チャンネルから集計（直近N日）────
    since = now - datetime.timedelta(days=days)
    ch_msg_counts = {}   # {channel_name: count}
    total_msgs    = 0
    active_chs    = 0
    for ch in guild.text_channels:
        count = 0
        try:
            async for msg in ch.history(after=since, limit=500):
                if not msg.author.bot:
                    count += 1
            if count > 0:
                active_chs += 1
                ch_msg_counts[ch.name] = count
                total_msgs += count
        except Exception:
            pass

    # 活動チャンネルTOP5
    top_chs = sorted(ch_msg_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # 過疎レベル判定
    msgs_per_day = total_msgs / max(days, 1)
    if msgs_per_day >= 200:
        kasso = "超活発"
        kasso_color = (87, 242, 135)
    elif msgs_per_day >= 50:
        kasso = "活発"
        kasso_color = (87, 200, 135)
    elif msgs_per_day >= 10:
        kasso = "普通"
        kasso_color = (254, 231, 92)
    elif msgs_per_day >= 1:
        kasso = "過疎気味"
        kasso_color = (237, 150, 69)
    else:
        kasso = "過疎"
        kasso_color = (237, 66, 69)

    # ── ロール・Boost統計 ────────────────────────────
    roles_count  = len(guild.roles) - 1
    boost_level  = guild.premium_tier
    boost_count  = guild.premium_subscription_count or 0

    # ── サーバー作成日・年齢 ──────────────────────────
    created_at  = guild.created_at
    age_days    = (now - created_at).days
    age_str     = f"{age_days // 365}年{(age_days % 365) // 30}ヶ月" if age_days >= 365 else f"{age_days}日"

    img = _build_stats_image(guild, {
        "total": total_members, "human": human_members,
        "bot": bot_members, "online": online_members,
        "idle": idle_members, "dnd": dnd_members,
        "text_ch": text_chs, "voice_ch": voice_chs,
        "categories": categories, "forum": forum_chs, "stage": stage_chs,
        "vc_users": vc_users, "roles": roles_count,
        "boost_lv": boost_level, "boost_ct": boost_count,
        "total_msgs": total_msgs, "active_chs": active_chs,
        "msgs_per_day": msgs_per_day, "top_chs": top_chs,
        "kasso": kasso, "kasso_color": kasso_color,
        "age_str": age_str, "days": days,
    })
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    await interaction.followup.send(file=discord.File(buf, "stats.png"))


def _build_stats_image(guild: discord.Guild, data: dict) -> Image.Image:
    import random as _rnd

    W, H   = 760, 620
    BG     = (15, 17, 25)
    PANEL  = (24, 28, 40)
    ACCENT = (88, 101, 242)
    GREEN  = (87, 242, 135)
    YELLOW = (254, 231, 92)
    RED    = (237, 66, 69)
    ORANGE = (237, 150, 69)
    WHITE  = (255, 255, 255)
    GRAY   = (140, 145, 165)
    BLUE   = (80, 160, 240)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # 背景グラデーション
    for yi in range(H):
        t = yi / H
        draw.line([(0,yi),(W,yi)], fill=(
            int(15+10*t), int(17+8*t), int(25+15*t)))

    # ノイズ
    for _ in range(2000):
        xi,yi = _rnd.randint(0,W-1), _rnd.randint(0,H-1)
        v = _rnd.randint(22,38)
        draw.point((xi,yi), fill=(v,v+2,v+8))

    f_xl = _load_font(26)
    f_lg = _load_font(20)
    f_md = _load_font(15)
    f_sm = _load_font(12)
    f_xs = _load_font(11)

    # ── タイトルバー ──────────────────────────────────
    draw.rectangle([0,0,W,50], fill=(ACCENT[0]//3, ACCENT[1]//3, ACCENT[2]//3+15))
    draw.rectangle([0,50,W,53], fill=ACCENT)
    draw.text((16, 25), f"{guild.name}  サーバー統計", font=f_xl, fill=WHITE, anchor="lm")
    draw.text((W-12, 25), f"直近{data['days']}日 / {datetime.datetime.now().strftime('%Y-%m-%d')}", font=f_xs, fill=GRAY, anchor="rm")

    # ── カード描画ヘルパー ────────────────────────────
    def card(x, y, w, h, title, value, color=WHITE, sub=""):
        draw.rounded_rectangle([x,y,x+w,y+h], radius=8, fill=PANEL)
        draw.rounded_rectangle([x,y,x+w,y+3], radius=2, fill=color)
        draw.text((x+10, y+15), title, font=f_xs, fill=GRAY, anchor="lm")
        draw.text((x+10, y+36), str(value), font=f_lg, fill=color, anchor="lm")
        if sub:
            draw.text((x+10, y+54), sub, font=f_xs, fill=GRAY, anchor="lm")

    gap = 10
    mx  = 12
    cw  = (W - mx*2 - gap*3) // 4   # 4列均等

    # ── 行1: メンバー系 ──────────────────────────────
    r1y = 62
    rh  = 72
    card(mx,            r1y, cw, rh, "総メンバー", data["total"], WHITE)
    card(mx+cw+gap,     r1y, cw, rh, "人間",       data["human"], GREEN,
         f"オンライン {data['online']}")
    card(mx+(cw+gap)*2, r1y, cw, rh, "席外し/DND", f"{data['idle']} / {data['dnd']}", YELLOW)
    card(mx+(cw+gap)*3, r1y, cw, rh, "Bot",        data["bot"],   GRAY)

    # ── 行2: チャンネル系 ────────────────────────────
    r2y = r1y + rh + gap
    card(mx,            r2y, cw, rh, "テキストch",  data["text_ch"],  ACCENT)
    card(mx+cw+gap,     r2y, cw, rh, "ボイスch",    data["voice_ch"], ACCENT,
         f"現在 {data['vc_users']} 人接続")
    card(mx+(cw+gap)*2, r2y, cw, rh, "カテゴリ",   data["categories"], GRAY)
    card(mx+(cw+gap)*3, r2y, cw, rh, "ロール数",    data["roles"],    YELLOW)

    # ── 行3: 活動統計 ────────────────────────────────
    r3y = r2y + rh + gap
    pw  = cw*2 + gap   # 2列幅パネル

    # 活動レベルパネル
    draw.rounded_rectangle([mx, r3y, mx+pw, r3y+rh], radius=8, fill=PANEL)
    draw.rounded_rectangle([mx, r3y, mx+pw, r3y+3], radius=2, fill=data["kasso_color"])
    draw.text((mx+10, r3y+15), "活動レベル", font=f_xs, fill=GRAY, anchor="lm")
    draw.text((mx+10, r3y+36), data["kasso"], font=f_lg, fill=data["kasso_color"], anchor="lm")
    draw.text((mx+pw-10, r3y+36), f"{data['msgs_per_day']:.1f} msg/日", font=f_sm, fill=GRAY, anchor="rm")

    # メッセージ統計パネル
    bx = mx+pw+gap
    draw.rounded_rectangle([bx, r3y, bx+pw, r3y+rh], radius=8, fill=PANEL)
    draw.rounded_rectangle([bx, r3y, bx+pw, r3y+3], radius=2, fill=BLUE)
    draw.text((bx+10, r3y+15), f"直近{data['days']}日のメッセージ数", font=f_xs, fill=GRAY, anchor="lm")
    draw.text((bx+10, r3y+36), f"{data['total_msgs']:,} 件", font=f_lg, fill=BLUE, anchor="lm")
    draw.text((bx+pw-10, r3y+36), f"活動ch {data['active_chs']}/{data['text_ch']}", font=f_sm, fill=GRAY, anchor="rm")

    # ── 行4: 活動TOP5チャンネル ──────────────────────
    r4y = r3y + rh + gap
    bh  = 150
    draw.rounded_rectangle([mx, r4y, mx+pw, r4y+bh], radius=8, fill=PANEL)
    draw.text((mx+10, r4y+14), f"チャンネル活動 TOP5 (直近{data['days']}日)", font=f_sm, fill=GRAY, anchor="lm")
    top = data["top_chs"]
    max_count = top[0][1] if top else 1
    for idx, (name, count) in enumerate(top):
        ty  = r4y + 32 + idx * 22
        bw2 = int((pw - 20) * count / max(max_count, 1))
        col = [GREEN, ACCENT, YELLOW, ORANGE, GRAY][idx]
        draw.rounded_rectangle([mx+10, ty, mx+10+bw2, ty+14], radius=3, fill=col)
        disp_name = f"#{name[:18]}"
        draw.text((mx+14, ty+7), disp_name, font=f_xs, fill=BG, anchor="lm")
        draw.text((mx+pw-10, ty+7), str(count), font=f_xs, fill=col, anchor="rm")
    if not top:
        draw.text((mx+pw//2, r4y+bh//2), "データなし", font=f_sm, fill=GRAY, anchor="mm")

    # ── Boost + サーバー情報パネル ───────────────────
    bx2 = mx+pw+gap
    draw.rounded_rectangle([bx2, r4y, bx2+pw, r4y+bh], radius=8, fill=PANEL)
    draw.text((bx2+10, r4y+14), "サーバー情報", font=f_sm, fill=GRAY, anchor="lm")
    boost_col = [GRAY, GREEN, ACCENT, YELLOW][min(data["boost_lv"],3)]
    infos = [
        ("ブーストLv",   f"Lv.{data['boost_lv']}  ({data['boost_ct']}件)", boost_col),
        ("サーバー歴",   data["age_str"],                                  WHITE),
        ("フォーラムch", str(data["forum"]),                               GRAY),
        ("ステージch",   str(data["stage"]),                               GRAY),
        ("作成日",       guild.created_at.strftime("%Y/%m/%d"),            GRAY),
    ]
    for ii, (label, val, col) in enumerate(infos):
        ty = r4y + 34 + ii * 22
        draw.text((bx2+10, ty), label, font=f_xs, fill=GRAY, anchor="lm")
        draw.text((bx2+pw-10, ty), val, font=f_xs, fill=col, anchor="rm")

    # ── フッター ──────────────────────────────────────
    draw.text((W//2, H-11), f"mamechosu bot  •  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
              font=f_xs, fill=GRAY, anchor="mm")

    return img

# ──────────────────────────────────────────────
# /supiki
# ──────────────────────────────────────────────
SUPIKI_LINES = [
    "ｳｱｱ!", "ｴｴｳ!", "ｳｴｴ!",
    "ｽﾋﾟｷﾃﾞﾙｼﾞﾊﾞｾﾞﾖ!", "ｽﾋﾟｷﾃﾞﾙｼﾞﾊﾞｯｾﾖ!", "ｽﾋﾟｷﾃﾘｼﾞﾏｾﾖ!",
    "ｽﾋﾟｷﾓﾘﾁｬﾊﾞﾀﾞﾝｷﾞｼﾞﾏｾﾖ!", "ｽﾋﾟｷｦｲｼﾞﾒﾇﾝﾃ!", "ﾁｮﾜﾖｰ",
    "ﾁｮﾜﾖ~", "ﾑﾙｺﾞﾙﾚｼﾞ", "ﾎﾊﾞｷﾞ", "ｽﾝﾊﾞｺｯﾁ",
    "ﾁｮﾝﾁｭﾄﾞﾝ", "ﾎﾊﾞｷｯｸ", "ｲｼﾞﾒﾇﾝﾃﾞ…",
]

async def _supiki_webhook(channel: discord.TextChannel):
    try:
        hooks = await channel.webhooks()
        wh = next((h for h in hooks if h.name == "ｽﾋﾟｷ"), None)
        if wh is None:
            img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img", "supiki.webp")
            avatar = None
            if os.path.exists(img_path):
                with open(img_path, "rb") as f:
                    avatar = f.read()
            wh = await channel.create_webhook(name="ｽﾋﾟｷ", avatar=avatar)
        return wh
    except Exception as e:
        pass
        return None

# ──────────────────────────────────────────────
# /grok
# ──────────────────────────────────────────────
@bot.tree.command(name="grok", description="grok_dc の GitHub リポジトリを表示します")
async def cmd_grok(interaction: discord.Interaction):
    await interaction.response.send_message(
        "https://github.com/soramame72/grok_dc/", ephemeral=False)


@bot.tree.command(name="supiki", description="ｽﾋﾟｷになります")
async def cmd_supiki(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    wh = await _supiki_webhook(interaction.channel)
    if wh is None:
        await interaction.followup.send("Webhookの作成に失敗しました。", ephemeral=True)
        return
    await wh.send(random.choice(SUPIKI_LINES), username="ｽﾋﾟｷ")
    await interaction.followup.send("ｽﾋﾟｷ!", ephemeral=True)


# ──────────────────────────────────────────────
# /permission
# ──────────────────────────────────────────────
@bot.tree.command(name="permission", description="Botの権限と状態を一覧表示します")
async def cmd_permission(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    me    = interaction.guild.me
    perms = me.guild_permissions
    checks = [
        ("管理者",           perms.administrator),
        ("チャンネル管理",   perms.manage_channels),
        ("ロール管理",       perms.manage_roles),
        ("メッセージ管理",   perms.manage_messages),
        ("サーバー管理",     perms.manage_guild),
        ("メッセージ送信",   perms.send_messages),
        ("埋め込みリンク",   perms.embed_links),
        ("ファイル添付",     perms.attach_files),
        ("リアクション追加", perms.add_reactions),
        ("Webhook管理",     perms.manage_webhooks),
        ("メンバー閲覧",     perms.view_audit_log),
    ]
    ok = [n for n, v in checks if v]
    ng = [n for n, v in checks if not v]
    color = 0x57F287 if not ng else (0xFEE75C if len(ng) <= 3 else 0xED4245)
    embed = discord.Embed(title=f"{me.display_name} の権限確認", color=color)
    embed.set_thumbnail(url=me.display_avatar.url)
    embed.add_field(name=f"付与済み ({len(ok)}件)", value="\n".join(f"[OK] {n}" for n in ok) or "なし", inline=True)
    if ng:
        embed.add_field(name=f"不足 ({len(ng)}件)", value="\n".join(f"[NG] {n}" for n in ng), inline=True)
    roles = ", ".join(r.name for r in me.roles if r.name != "@everyone") or "なし"
    embed.add_field(name="付与ロール", value=roles, inline=False)
    embed.add_field(name="Ping", value=f"{round(bot.latency*1000,1)} ms", inline=True)
    embed.set_footer(text=f"Bot ID: {me.id}")
    await interaction.followup.send(embed=embed)


# ──────────────────────────────────────────────
# /quote
# ──────────────────────────────────────────────
QUOTE_THEMES = {
    "dark":  {"bg":(28,28,35),    "fg":(235,225,200), "accent":(180,140,80),  "sub":(140,130,115)},
    "light": {"bg":(250,247,238), "fg":(45,35,25),    "accent":(120,80,40),   "sub":(160,140,110)},
    "blue":  {"bg":(18,32,55),    "fg":(220,230,245), "accent":(80,140,210),  "sub":(120,150,190)},
    "green": {"bg":(22,45,32),    "fg":(220,240,225), "accent":(80,180,110),  "sub":(120,170,135)},
    "red":   {"bg":(45,18,22),    "fg":(245,225,220), "accent":(200,80,70),   "sub":(170,120,115)},
}

import re as _re_md

# Twemoji絵文字パターン（Unicode範囲）
_EMOJI_PATTERN = _re_md.compile(
    r'[\U0001F300-\U0001F9FF'
    r'\U0001FA00-\U0001FAFF'
    r'\U00002600-\U000027BF'
    r'\U0001F1E0-\U0001F1FF'  # 国旗
    r']+(?:\uFE0F|\u200D[\U0001F300-\U0001F9FF])*'
)

def _clean_markdown_only(text: str) -> str:
    """Discordマークダウン記号のみ除去。絵文字（Unicode・カスタム）はそのまま保持。"""
    text = _re_md.sub(r"```[\s\S]*?```", "", text)
    text = _re_md.sub(r"`[^`]*`", "", text)
    text = _re_md.sub(r"^#{1,6}\s+", "", text, flags=_re_md.MULTILINE)
    text = _re_md.sub(r"\*{1,3}([^*\n]*)\*{1,3}", r"\1", text)
    text = _re_md.sub(r"_{1,2}([^_\n]*)_{1,2}", r"\1", text)
    text = _re_md.sub(r"~~([^~]*)~~", r"\1", text)
    text = _re_md.sub(r"^>\s?", "", text, flags=_re_md.MULTILINE)
    return text.strip()

_CUSTOM_EMOJI_PATTERN = _re_md.compile(r"<(a?):([^:>]+):(\d+)>")

async def _fetch_emoji_images(text: str, guild: "discord.Guild | None" = None) -> dict:
    """Unicode絵文字(Twemoji)とカスタム絵文字(Discord CDN)をダウンロード"""
    result = {}
    try:
        async with aiohttp.ClientSession() as session:
            for m in _CUSTOM_EMOJI_PATTERN.finditer(text):
                animated, name, eid = m.group(1), m.group(2), m.group(3)
                key = m.group(0)
                if key in result:
                    continue
                ext = "gif" if animated else "png"
                url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=64"
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                        if resp.status == 200:
                            result[key] = await resp.read()
                except Exception:
                    pass

            for emoji_char in set(_EMOJI_PATTERN.findall(text)):
                cps = [f"{ord(c):x}" for c in emoji_char if ord(c) != 0xFE0F]
                cp_str = "-".join(cps)
                urls = [
                    f"https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{cp_str}.png",
                    f"https://twemoji.maxcdn.com/v/latest/72x72/{cp_str}.png",
                ]
                try:
                    for url in urls:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                            if resp.status == 200:
                                result[emoji_char] = await resp.read()
                                break
                except Exception:
                    pass
    except Exception:
        pass
    return result

def _split_for_render(text: str) -> list:
    """テキストを ('text', str) | ('emoji', str) のセグメントリストに分割。カスタム絵文字にも対応。"""
    import re as _re2
    combined = _re2.compile(
        r"(<a?:[^:>]+:\d+>)"
        r"|"
        r"([🌀-🧿🨀-🫿☀-➿🇠-🇿]"
        r"+(?:️|‍[🌀-🧿])*)"
    )
    segments = []
    last = 0
    for m in combined.finditer(text):
        if m.start() > last:
            segments.append(('text', text[last:m.start()]))
        segments.append(('emoji', m.group()))
        last = m.end()
    if last < len(text):
        segments.append(('text', text[last:]))
    return segments

def _measure_seg(seg_type: str, content: str, font, emoji_size: int) -> int:
    """セグメントの描画幅を返す"""
    if seg_type == 'emoji':
        return emoji_size + 2
    try:
        bb = font.getbbox(content)
        return bb[2] - bb[0]
    except Exception:
        return len(content) * font.size

def _draw_emoji_line(draw, img, text: str, font, cx: int, y: int, fill, emoji_images: dict, emoji_size: int = None):
    """絵文字混在の1行テキストをcxを中心に描画する（絵文字はダウンロード済み画像があれば画像として貼り付け）"""
    if emoji_images is None:
        emoji_images = {}
    if emoji_size is None:
        emoji_size = max(getattr(font, "size", 18), 18)
    segs = _split_for_render(text)
    total_w = 0
    for st, ct in segs:
        total_w += _measure_seg(st, ct, font, emoji_size)
    x = cx - total_w // 2
    for st, ct in segs:
        if st == "emoji":
            em_data = emoji_images.get(ct)
            if em_data:
                try:
                    em_img = Image.open(BytesIO(em_data)).convert("RGBA")
                    em_img = em_img.resize((emoji_size, emoji_size), Image.Resampling.LANCZOS)
                    img.paste(em_img, (x, y), mask=em_img)
                    x += emoji_size + 2
                    continue
                except Exception:
                    pass
            draw.text((x, y), ct, font=font, fill=fill)
            x += _measure_seg("text", ct, font, emoji_size)
        else:
            draw.text((x, y), ct, font=font, fill=fill)
            x += _measure_seg("text", ct, font, emoji_size)

def _wrap_mixed(text: str, font, emoji_size: int, max_width: int) -> list:
    """テキストを行ごとのセグメントリスト（list[list[tuple]]）に変換"""
    all_lines = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            all_lines.append([])
            continue
        raw_segs = _split_for_render(paragraph)
        cur_line = []
        cur_w = 0
        for seg_type, content in raw_segs:
            if seg_type == 'emoji':
                w = emoji_size + 2
                if cur_w + w > max_width and cur_line:
                    all_lines.append(cur_line)
                    cur_line = []
                    cur_w = 0
                cur_line.append(('emoji', content))
                cur_w += w
            else:
                for char in content:
                    try:
                        cw = font.getbbox(char)[2]
                    except Exception:
                        cw = max(font.size // 2, 4)
                    if cur_w + cw > max_width and cur_line:
                        all_lines.append(cur_line)
                        cur_line = []
                        cur_w = 0
                    if cur_line and cur_line[-1][0] == 'text':
                        cur_line[-1] = ('text', cur_line[-1][1] + char)
                    else:
                        cur_line.append(('text', char))
                    cur_w += cw
        if cur_line:
            all_lines.append(cur_line)
    return all_lines

def build_quote_image(text: str, author_name: str = "", avatar_bytes: bytes = b"",
                      theme_name: str = "dark", emoji_images: dict = None,
                      username: str = "", color: bool = False) -> Image.Image:
    if emoji_images is None:
        emoji_images = {}
    text = _clean_markdown_only(text) or " "

    W, H   = 1200, 630
    BG     = (8, 8, 8)
    FG     = (245, 245, 245)
    SUB    = (130, 130, 130)
    ACCENT = (220, 220, 220)
    SEP    = (38, 38, 38)

    font_paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
        "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Geneva.ttf",
    ]
    def load_font(size: int):
        for fp in font_paths:
            if os.path.exists(fp):
                try: return ImageFont.truetype(fp, size)
                except Exception: continue
        return ImageFont.load_default()

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── 左: アバター（縦full + 右フェード / グレーorカラー）─
    AV_W = int(W * 0.40)
    has_avatar = False
    if avatar_bytes:
        try:
            av = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
            aw, ah = av.size
            # 縦をHに合わせてcoverスケール、横は中央クロップ
            scale = H / ah
            new_w = int(aw * scale)
            av = av.resize((max(new_w, AV_W), H), Image.Resampling.LANCZOS)
            nw, nh = av.size
            av = av.crop(((nw - AV_W) // 2, 0, (nw - AV_W) // 2 + AV_W, H))

            if color:
                av_base = av.convert("RGB")
            else:
                av_base = av.convert("L").convert("RGB")

            # 右端フェード (58%→100%)
            fade = Image.new("L", (AV_W, H), 255)
            fade_start = int(AV_W * 0.58)
            fd = ImageDraw.Draw(fade)
            for fx in range(fade_start, AV_W):
                t = (fx - fade_start) / (AV_W - fade_start)
                fd.line([(fx, 0), (fx, H)], fill=max(0, int(255 * (1 - t ** 1.4))))

            from PIL import ImageChops as _IC
            blended = Image.composite(
                Image.new("RGB", (AV_W, H), BG),
                av_base,
                _IC.invert(fade)
            )
            img.paste(blended, (0, 0))
            has_avatar = True
        except Exception:
            pass

    # ── 縦セパレーター ────────────────────────────────────
    SEP_X = AV_W if has_avatar else 0
    if has_avatar:
        for y in range(H):
            t = abs(y / H - 0.5) * 2          # 0(中央)→1(端)
            alpha = int(SEP[0] * (1 - t ** 2))
            draw.point((SEP_X, y), fill=(alpha, alpha, alpha))

    # ── 右: テキストエリア ────────────────────────────────
    PAD_L  = 64 if has_avatar else 90
    PAD_R  = 64
    TX     = SEP_X + PAD_L
    TRX    = W - PAD_R
    TW     = TRX - TX
    TY_TOP = 68
    TY_BOT = H - 118
    TH     = TY_BOT - TY_TOP

    # フォントサイズ自動決定
    best_font, best_lines, best_size = None, [], 18
    for fs in range(68, 17, -2):
        f  = load_font(fs)
        em = max(fs, 22)
        lines = _wrap_mixed(text, f, em, TW)
        lh = int(fs * 1.62)
        if lh * len(lines) <= TH:
            best_font, best_lines, best_size = f, lines, fs
            break
    if best_font is None:
        best_font  = load_font(17)
        best_size  = 17
        best_lines = _wrap_mixed(text, best_font, 17, TW)

    emoji_size = max(best_size, 24)
    line_h     = int(best_size * 1.62)
    total_h    = line_h * len(best_lines)
    cx         = TX + TW // 2

    def seg_width(segs):
        w = 0
        for st, ct in segs:
            if st == "emoji":
                w += emoji_size + int(best_size * 0.14)
            else:
                try: w += best_font.getbbox(ct)[2] - best_font.getbbox(ct)[0]
                except: w += best_size * len(ct)
        return w

    y = TY_TOP + (TH - total_h) // 2
    for line_segs in best_lines:
        lw = seg_width(line_segs)
        x  = cx - lw // 2
        for seg_type, content in line_segs:
            if seg_type == "emoji":
                em_data = emoji_images.get(content)
                if em_data:
                    try:
                        em_img = Image.open(BytesIO(em_data)).convert("RGBA")
                        em_img = em_img.resize((emoji_size, emoji_size), Image.Resampling.LANCZOS)
                        img.paste(em_img, (x, y + (line_h - emoji_size) // 2), mask=em_img)
                        x += emoji_size + int(best_size * 0.14)
                        continue
                    except Exception:
                        pass
                draw.text((x, y), content, font=best_font, fill=FG)
                try: x += best_font.getbbox(content)[2] - best_font.getbbox(content)[0]
                except: x += best_size
            else:
                draw.text((x, y), content, font=best_font, fill=FG)
                try: x += best_font.getbbox(content)[2] - best_font.getbbox(content)[0]
                except: x += best_size * len(content)
        y += line_h

    # ── 著者ブロック ──────────────────────────────────────
    # 細い水平線
    sep_y = TY_BOT + 10
    draw.rectangle([cx - 60, sep_y, cx + 60, sep_y + 1], fill=SEP)

    author_font   = load_font(26)
    username_font = load_font(17)
    ay = sep_y + 14
    if author_name:
        _draw_emoji_line(draw, img, f"— {author_name}", author_font, cx, ay, ACCENT, emoji_images)
        ay += 34
    if username:
        _draw_emoji_line(draw, img, f"@{username}", username_font, cx, ay, SUB, emoji_images)

    return img

async def _make_quote_file(text: str, author_name: str, avatar_bytes: bytes = b"",
                           theme_name: str = "dark", guild: "discord.Guild | None" = None,
                           username: str = "", color: bool = False) -> discord.File:
    emoji_images = await _fetch_emoji_images(f"{text}\n{author_name}\n{username}", guild=guild)
    img = build_quote_image(text, author_name, avatar_bytes,
                            theme_name=theme_name, emoji_images=emoji_images, username=username,
                            color=color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename="quote.png")

@bot.tree.command(name="quote", description="名言カード画像を生成します")
@app_commands.describe(text="名言の本文（200文字以内）", author="著者名（省略可）",
                       theme="dark/light/blue/green/red（デフォルト:dark）",
                       color="アイコンをカラーで表示するか（デフォルト:グレー）")
async def cmd_quote(interaction: discord.Interaction, text: str, author: str = "",
                    theme: str = "dark", color: bool = False):
    await safe_defer(interaction)
    if len(text) > 200:
        await interaction.followup.send("200文字以内で入力してください。", ephemeral=True); return

    author_name = author or interaction.user.display_name
    avatar_bytes = b""
    if not author and interaction.user.display_avatar:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(interaction.user.display_avatar.url) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
        except: pass

    uname = interaction.user.name if not author else ""
    file = await _make_quote_file(text, author_name, avatar_bytes, theme_name=theme, guild=interaction.guild, username=uname, color=color)
    await interaction.followup.send(file=file)

@bot.tree.command(name="meigen", description="過去のメッセージからAIが名言/迷言を発掘して名言カード画像を生成します")
@app_commands.describe(
    quote_type="発掘する種類（デフォルト: 迷言）",
    channel="検索するチャンネル（省略=現在のチャンネル）"
)
@app_commands.choices(quote_type=[
    app_commands.Choice(name="迷言（面白い発言）", value="funny"),
    app_commands.Choice(name="名言（良い発言）", value="good")
])
async def cmd_meigen(interaction: discord.Interaction,
                     quote_type: app_commands.Choice[str] = None,
                     channel: discord.TextChannel = None):
    await safe_defer(interaction)
    if not interaction.guild:
        await interaction.followup.send("サーバー内でのみ使用できます。", ephemeral=True); return

    target_ch = channel or interaction.channel
    q_type = quote_type.value if quote_type else "funny"

    # 実在するメッセージを最大1000件収集
    raw_msgs = []
    try:
        async for msg in target_ch.history(limit=1000):
            if msg.author.bot or not msg.content.strip():
                continue
            # URLのみ・コマンドのみは除外
            c = msg.content.strip()
            if c.startswith("/") or c.startswith("http"):
                continue
            raw_msgs.append({
                "display": msg.author.display_name,
                "name":    msg.author.name,
                "content": c,
                "member":  interaction.guild.get_member(msg.author.id),
                "id":      msg.id,
                "url":     msg.jump_url,
                "avatar_bytes": b""
            })
    except Exception as e:
        await interaction.followup.send(f"履歴取得エラー: {e}", ephemeral=True); return

    if len(raw_msgs) < 3:
        await interaction.followup.send("会話履歴が少なすぎます。", ephemeral=True); return

    # ランダムに最大100件をサンプリング（同じ発言ばかり選ばれるのを防ぐ）
    import random
    sample_size = min(len(raw_msgs), 100)
    sampled = random.sample(raw_msgs, sample_size)
    
    import json as _json

    api_key = get_groq_api_key(interaction.guild_id)
    if not api_key:
        await interaction.followup.send("Groq APIキーが設定されていません。", ephemeral=True); return

    async def _call_groq(log_lines: list[str]) -> dict | None:
        history_text = "\n".join(log_lines)
        if q_type == "funny":
            system_p = (
                "あなたはDiscordの会話ログから「迷言」を発掘するAIです。\n"
                "迷言とは：面白い・ズレてる・哲学っぽい・笑える・思わず二度見するような発言のことです。\n"
                "必ずログの中から1件選んでください。選べない理由は存在しません。\n"
                "出力はJSON形式のみ。前置き・説明・```は不要です。"
            )
            user_p = (
                "以下の会話ログから最も迷言らしい発言を1つ選び、JSONで返してください。\n"
                "形式: {\"index\": 番号, \"text\": \"発言内容（原文のまま）\", \"author\": \"発言者名\"}\n\n"
                f"会話ログ:\n{history_text}"
            )
        else:
            system_p = (
                "あなたはDiscordの会話ログから「名言」を発掘するAIです。\n"
                "名言とは：心に響く・素晴らしい・教訓になる・感動的な発言のことです。\n"
                "必ずログの中から1件選んでください。選べない理由は存在しません。\n"
                "出力はJSON形式のみ。前置き・説明・```は不要です。"
            )
            user_p = (
                "以下の会話ログから最も名言らしい発言を1つ選び、JSONで返してください。\n"
                "形式: {\"index\": 番号, \"text\": \"発言内容（原文のまま）\", \"author\": \"発言者名\"}\n\n"
                f"会話ログ:\n{history_text}"
            )
            
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": "openai/gpt-oss-120b",
                        "messages": [
                            {"role": "system", "content": system_p},
                            {"role": "user",   "content": user_p},
                        ],
                        "max_tokens": 200,
                        "temperature": 0.8,
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    # リソース表示用にレートリミットを保存
                    req_rem = resp.headers.get("x-ratelimit-remaining-requests", "N/A")
                    req_lim = resp.headers.get("x-ratelimit-limit-requests", "N/A")
                    tok_rem = resp.headers.get("x-ratelimit-remaining-tokens", "N/A")
                    tok_lim = resp.headers.get("x-ratelimit-limit-tokens", "N/A")
                    bot._groq_ratelimit = {
                        "req_rem": req_rem, "req_lim": req_lim,
                        "tok_rem": tok_rem, "tok_lim": tok_lim
                    }
                    
                    if resp.status == 200:
                        data = await resp.json()
                        raw = data["choices"][0]["message"]["content"].strip()
                        # コードフェンス除去（```json ... ``` や ``` ... ``` 形式に対応）
                        import re as _re
                        raw_clean = _re.sub(r"^```(?:json)?\s*", "", raw, flags=_re.MULTILINE)
                        raw_clean = _re.sub(r"```\s*$", "", raw_clean, flags=_re.MULTILINE).strip()
                        try:
                            return _json.loads(raw_clean)
                        except Exception:
                            # JSONが複数ある場合は最初の{}ブロックだけ抽出して試みる
                            m = _re.search(r'\{[^{}]+\}', raw_clean, _re.DOTALL)
                            if m:
                                try:
                                    return _json.loads(m.group(0))
                                except Exception:
                                    pass
                    elif resp.status == 429:
                        pass  # レートリミット: フォールバックへ
        except Exception:
            pass
        return None

    # サンプリングしたものをGroqに渡す
    log_lines = [f"[{i}] {m['display']}: {m['content'][:100]}" for i, m in enumerate(sampled)]
    parsed = await _call_groq(log_lines)
    
    selected_entry = None
    if parsed:
        idx = parsed.get("index")
        if idx is not None:
            try:
                idx = int(idx)
                if 0 <= idx < len(sampled):
                    selected_entry = sampled[idx]
            except Exception:
                pass
                
    if not selected_entry:
        # Groqが失敗した場合はランダムに1つ選ぶフォールバック
        selected_entry = random.choice(sampled)
        
    selected_text   = selected_entry["content"]
    selected_author = selected_entry["display"]
    selected_member = selected_entry["member"]
    selected_msg_id = selected_entry["id"]
    selected_url    = selected_entry["url"]
    
    if not selected_text:
        await interaction.followup.send("迷言/名言が見つかりませんでした。", ephemeral=True); return

    avatar_bytes = b""
    if selected_member and selected_member.display_avatar:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(selected_member.display_avatar.url) as resp:
                    if resp.status == 200:
                        avatar_bytes = await resp.read()
        except: pass

    uname = selected_member.name if selected_member else ""
    file = await _make_quote_file(selected_text, selected_author or "不明", avatar_bytes,
                                  guild=interaction.guild, username=uname)

    # メッセージ直リンク
    content = selected_url if selected_url else None
    await interaction.followup.send(content=content, file=file)

def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """テキストをmax_widthに収まるように折り返す。改行文字も尊重する。"""
    lines_out = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines_out.append("")
            continue
        cur = ""
        for char in paragraph:
            test = cur + char
            try:
                w = font.getbbox(test)[2]
            except Exception:
                w = len(test) * font.size
            if w > max_width and cur:
                lines_out.append(cur)
                cur = char
            else:
                cur = test
        if cur:
            lines_out.append(cur)
    return lines_out

# ──────────────────────────────────────────────
# /purge
# ──────────────────────────────────────────────
@bot.tree.command(name="purge", description="直近N件のメッセージを削除します（最大100件）")
@app_commands.describe(count="削除するメッセージ数（1〜100）")
async def cmd_purge(interaction: discord.Interaction, count: int):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("メッセージ管理権限が必要です。", ephemeral=True); return
    if not 1 <= count <= 100:
        await interaction.followup.send("1〜100の範囲で指定してください。", ephemeral=True); return
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.followup.send("テキストチャンネルでのみ使用できます。", ephemeral=True); return
    try:
        deleted = await interaction.channel.purge(limit=count)
        await interaction.followup.send(f"{len(deleted)} 件削除しました。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"削除に失敗しました: {e}", ephemeral=True)


# ──────────────────────────────────────────────
# /globalchat
# ──────────────────────────────────────────────
def get_global_channels() -> list:
    channels = db_read("globalchat", shared="channels")
    return channels if isinstance(channels, list) else []

def set_global_channels(channels: list):
    db_write("globalchat", channels, shared="channels")

async def get_or_create_webhook(channel: discord.TextChannel):
    try:
        for h in await channel.webhooks():
            if h.name == "GlobalChat": return h.url
        return (await channel.create_webhook(name="GlobalChat")).url
    except Exception: return None

class GlobalChatTosView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="同意する", style=discord.ButtonStyle.green, custom_id="globalchat_tos_agree")
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        agreed_users = db_read("globalchat", shared="agreed_users")
        if not isinstance(agreed_users, list):
            agreed_users = []
        if user_id not in agreed_users:
            agreed_users.append(user_id)
            db_write("globalchat", agreed_users, shared="agreed_users")
        
        await interaction.response.edit_message(content="利用規約に同意しました。グローバルチャットをご利用いただけます！", view=None)

_global_user_blocks = {}
_global_user_last_content = {}
_global_user_timestamps = {}

async def relay_global_message(message: discord.Message):
    channels = get_global_channels()
    if not any(c["channel_id"] == message.channel.id for c in channels): return
    if message.author.bot: return   # Bot発言はリレーしない

    user_id = message.author.id
    now = time.time()

    # 1. メンション禁止（ユーザー・everyone/here・ロール）
    if message.mentions or message.role_mentions or message.mention_everyone:
        try:
            await message.delete()
        except: pass
        try:
            await message.channel.send(
                f"{message.author.mention} グローバルチャットではメンション（ユーザー・everyone/here・ロール）を送信できません。",
                delete_after=8)
        except: pass
        return

    # 2. スパム対策（一時ブロックチェック）
    if _global_user_blocks.get(user_id, 0) > now:
        try:
            await message.delete()
        except: pass
        return

    # 3. 利用規約同意チェック
    agreed_users = db_read("globalchat", shared="agreed_users")
    if not isinstance(agreed_users, list):
        agreed_users = []
    
    if user_id not in agreed_users:
        try:
            await message.delete()
        except: pass
        
        tos_text = (
            "**[グローバルチャット利用規約]**\n\n"
            "グローバルチャットをご利用いただくには、以下の利用規約に同意する必要があります。\n\n"
            "1. スパム発言、嫌がらせ、連投、宣伝等のスパム行為を行わないこと\n"
            "2. 誹謗中傷、公序良俗に反するコンテンツ、個人情報の送信を行わないこと\n"
            "3. サーバー規約に準拠した発言を行うこと\n"
            "4. 管理者が不適切と判断した発言やユーザーは、事前の予告なく利用制限（規制）される場合があります\n\n"
            "規約に同意してチャットを送信しますか？"
        )
        view = GlobalChatTosView()
        try:
            await message.channel.send(f"{message.author.mention}\n{tos_text}", view=view, delete_after=120)
        except Exception:
            pass
        return

    # 4. 連投・スパム対策（レート制限と内容重複チェック）
    # クールダウン（3秒に1回）
    last_send = _global_user_timestamps.get(user_id, 0.0)
    if now - last_send < 3.0:
        try:
            await message.delete()
        except: pass
        
        # 連続警告でブロック
        violations = getattr(message.author, "_global_spam_violations", 0) + 1
        message.author._global_spam_violations = violations
        if violations >= 3:
            _global_user_blocks[user_id] = now + 300  # 5分ブロック
            message.author._global_spam_violations = 0
            try:
                await message.channel.send(
                    f"{message.author.mention} [警告] 連投スパムが検出されたため、5分間グローバルチャットの利用を一時停止（ブロック）しました。",
                    delete_after=8)
            except: pass
        else:
            try:
                await message.channel.send(
                    f"{message.author.mention} [警告] グローバルチャットへの送信速度が早すぎます。少し時間をおいてから送信してください。",
                    delete_after=8)
            except: pass
        return
    
    # 内容重複（10秒以内に同じ内容）
    last_content, last_content_time = _global_user_last_content.get(user_id, ("", 0.0))
    clean_content = message.content.strip() if message.content else ""
    if clean_content and clean_content == last_content and now - last_content_time < 10.0:
        try:
            await message.delete()
        except: pass
        try:
            await message.channel.send(
                f"{message.author.mention} [警告] 10秒以内に同じ内容のメッセージを連投することはできません。",
                delete_after=8)
        except: pass
        return

    # クールダウンと最終発言内容の更新
    _global_user_timestamps[user_id] = now
    if clean_content:
        _global_user_last_content[user_id] = (clean_content, now)
        if hasattr(message.author, "_global_spam_violations"):
            message.author._global_spam_violations = 0

    if not _check_rate(f"globalchat:{message.guild.id}", cooldown_sec=2.0):
        try:
            await message.delete()
        except: pass
        return

    content = (message.content or "") + "".join(f"\n{a.url}" for a in message.attachments)
    if not content.strip() or content.startswith("http"): return
    uname  = f"{message.author.display_name} @ {message.guild.name}"
    avatar = message.author.display_avatar.url
    async with aiohttp.ClientSession() as session:
        for c in channels:
            if c["channel_id"] == message.channel.id: continue
            try:
                async with session.post(c["webhook_url"],
                    json={"username": uname, "avatar_url": avatar, "content": content[:2000],
                          "allowed_mentions": {"parse": []}},
                    timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 404:
                        remaining = [x for x in get_global_channels() if x.get("webhook_url") != c["webhook_url"]]
                        set_global_channels(remaining)
            except Exception: pass

@bot.tree.command(name="globalchat", description="グローバルチャットの参加/退出を管理します")
@app_commands.describe(action="join=参加 / leave=退出 / list=一覧")
async def cmd_globalchat(interaction: discord.Interaction, action: str):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True); return
    ch = interaction.channel
    if not isinstance(ch, discord.TextChannel):
        await interaction.followup.send("テキストチャンネルで実行してください。", ephemeral=True); return
    channels = get_global_channels()
    if action == "join":
        if any(c["channel_id"] == ch.id for c in channels):
            await interaction.followup.send("すでに参加中です。", ephemeral=True); return
        wh = await get_or_create_webhook(ch)
        if not wh:
            await interaction.followup.send("Webhook作成失敗。「ウェブフックの管理」権限を確認してください。", ephemeral=True); return
        channels.append({"guild_id": interaction.guild_id, "channel_id": ch.id,
                         "guild_name": interaction.guild.name, "channel_name": ch.name, "webhook_url": wh})
        set_global_channels(channels)
        await interaction.followup.send(f"#{ch.name} をグローバルチャットに追加しました。({len(channels)}件参加中)", ephemeral=True)
    elif action == "leave":
        new = [c for c in channels if c["channel_id"] != ch.id]
        if len(new) == len(channels):
            await interaction.followup.send("このチャンネルは参加していません。", ephemeral=True); return
        set_global_channels(new)
        await interaction.followup.send(f"#{ch.name} をグローバルチャットから退出しました。", ephemeral=True)
    elif action == "list":
        if not channels:
            await interaction.followup.send("参加チャンネルはありません。", ephemeral=True); return
        lines = "\n".join(f"- {c['guild_name']} / #{c['channel_name']}" for c in channels)
        await interaction.followup.send(f"参加チャンネル ({len(channels)}件):\n{lines}", ephemeral=True)
    else:
        await interaction.followup.send("action は join / leave / list を指定してください。", ephemeral=True)


# ──────────────────────────────────────────────
# 熱盛検知
# ──────────────────────────────────────────────
ATSUMORI_KEYWORDS = ["熱盛", "あつもり", "アツモリ", "ATSUMORI", "atsumori"]

async def _groq_check_atsumori(text: str, guild_id: int = None) -> bool:
    api_key = get_groq_api_key(guild_id)
    if not api_key:
        return any(w in text for w in ATSUMORI_KEYWORDS)
    try:
        prompt = (
            "あなたはテレビ朝日報道ステーションの熱盛コーナーの審査員です。"
            "次のメッセージが熱盛かどうか判定してください。\n\n"
            "「はい」にする条件（以下のどちらかを満たす場合）:\n"
            "1. 「熱盛」「あつもり」「アツモリ」「ATSUMORI」などのキーワードが含まれており、内容が激しい感情・興奮・熱中・すごいプレーなどを感じさせる\n"
            "2. キーワードがなくても、内容が非常に熱くて盛り上がっており、明らかに熱盛コーナーにふさわしい場面（大逆転・劇的な出来事・極限の情熱など）である\n\n"
            "「いいえ」にする条件（以下に当てはまるなら「いいえ」）:\n"
            "- 完全に平凡な日常会話・挨拶・事務連絡で感情の昂りが全くない\n\n"
            f"メッセージ: 「{text}」\n\n"
            "熱盛なら「はい」、そうでなければ「いいえ」とだけ答えてください。"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.3,
                },
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data["choices"][0]["message"]["content"].strip()
                    return "はい" in res
    except Exception:
        pass
    return any(w in text for w in ATSUMORI_KEYWORDS)

async def check_atsumori(message: discord.Message):
    """熱盛キーワードを検知してatsumori.pngを送信する（1%の確率でランダム誤検知あり）"""
    text = message.content.strip()
    if not text or text.startswith("/") or text.startswith("http"):
        return

    is_real    = await _groq_check_atsumori(text)
    # 1%の確率で誤検知（本物ではない場合のみ）
    is_mistake = (not is_real) and (random.random() < 0.01)

    if not is_real and not is_mistake:
        return

    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atsumori.png")
    if not os.path.exists(img_path):
        return

    sent = await message.channel.send(
        file=discord.File(img_path, "atsumori.png"),
        reference=message,
    )
    if is_mistake:
        await sent.reply("失礼しました。熱盛が出てしまいました")


@bot.tree.command(name="atsumori", description="熱盛検知機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF",
                       channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_atsumori(interaction: discord.Interaction,
                       scope: str = "channel", state: str = "ON",
                       channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("atsumori", guild_id=interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["server"] = on
        msg = f"サーバー全体の熱盛検知を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["channels"] = chs
        msg = f"{target.mention} の熱盛検知を {'ON' if on else 'OFF'} にしました。"
    db_write("atsumori", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# ローマ字翻訳 (romaji)
# ──────────────────────────────────────────────
async def _groq_translate_romaji(text: str, api_key: str) -> str:
    try:
        system_prompt = (
            "あなたはローマ字（ヘボン式・訓令式・口語混じり）を自然な日本語に変換するエキスパートです。"
            "出力は変換結果の日本語テキストだけにしてください。前置き・説明・括弧書き・英語訳は一切不要です。"
        )
        user_prompt = (
            f"次のローマ字を自然な日本語に変換してください。\n"
            "・漢字・ひらがな・カタカナを文脈に合わせて使い分けてください。\n"
            "・スラング・略語・数字混じりも口語的に自然に変換してください。\n"
            "・英語がそのまま使われている単語（例: PC、SNS）はカタカナまたはそのままにしてください。\n"
            "・変換結果だけを返してください。\n\n"
            f"ローマ字: {text}"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    "max_tokens": 150,
                    "temperature": 0.1,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data["choices"][0]["message"]["content"].strip()
                    result = result.strip("「」『』\"'")
                    return result if result else None
    except Exception:
        pass
    return None

@bot.tree.command(name="romaji", description="ローマ字翻訳機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF",
                       channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_romaji(interaction: discord.Interaction,
                       scope: str = "channel", state: str = "ON",
                       channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("romaji", guild_id=interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["server"] = on
        msg = f"サーバー全体のローマ字翻訳機能を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["channels"] = chs
        msg = f"{target.mention} のローマ字翻訳機能を {'ON' if on else 'OFF'} にしました。"
    db_write("romaji", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# なりすまし (impersonate)
# ──────────────────────────────────────────────
@bot.tree.command(name="impersonate", description="指定したユーザーになりすまして発言します")
@app_commands.describe(user="なりすますユーザー", message="発言するメッセージ", attachment="添付する画像等(省略可)")
@app_commands.rename(user="ユーザー")
async def cmd_impersonate(interaction: discord.Interaction, user: discord.User, message: str, attachment: discord.Attachment = None):
    await safe_defer(interaction, ephemeral=True)

    gd = db_read("impersonate", guild_id=interaction.guild_id)
    if not (gd.get("server", True) or interaction.channel.id in gd.get("channels", [])):
        await interaction.followup.send("このチャンネルではなりすまし機能がOFFになっています。", ephemeral=True)
        return

    try:
        # ユーザー情報を取得
        avatar_url = user.display_avatar.url if user.display_avatar else user.default_avatar.url
        name = user.display_name

        # Webhookを使ってなりすまし発言
        whs = await interaction.channel.webhooks()
        wh = discord.utils.find(lambda w: w.name == "MamechosuImpersonate", whs)
        if not wh:
            wh = await interaction.channel.create_webhook(name="MamechosuImpersonate")
            
        file = await attachment.to_file() if attachment else discord.utils.MISSING
        sent_msg = await wh.send(content=message, username=name, avatar_url=avatar_url, wait=True, file=file)

        import time
        log_data = db_read("impersonate_log", guild_id=interaction.guild_id)
        if not isinstance(log_data, list):
            log_data = []
        log_data.append({
            "timestamp": time.time(),
            "executor": interaction.user.display_name,
            "target": name,
            "content": message,
            "url": sent_msg.jump_url if sent_msg else "Unknown"
        })
        thirty_days_ago = time.time() - 30 * 24 * 60 * 60
        log_data = [log for log in log_data if log["timestamp"] > thirty_days_ago]
        db_write("impersonate_log", log_data, guild_id=interaction.guild_id)

        # バレ确率を取得 (DBに保存されていなければデフォルト 10%)
        expose_rate = gd.get("expose_rate", 10)  # 1〜100 の整数％10%=10
        will_expose = random.randint(1, 100) <= expose_rate

        if will_expose:
            is_self = (user.id == interaction.user.id)
            if is_self:
                expose_msgs = [
                    f"あれ？ {interaction.user.mention} 自分で自分になりすまして何してんだこいつｗｗｗｗｗ",
                    f"ちょ待てｗｗｗ {interaction.user.mention} が自分自身になりすましてて草生える",
                    f"( ﾟДﾟ)　{interaction.user.mention} 、お前は一体誰になりすましたかったんだ…自分にか…",
                    f"自演乙　{interaction.user.mention} 自分のフリして自分のフリするとかどういうこと？",
                    f"むなしくないの…？ {interaction.user.mention} が自分自身になりすましてるんだが",
                    f"　∧_∧\n( ´∀｀) < {interaction.user.mention} が自分で自分をなりすまし！\n(　　)   お前は何と戦ってるんだｗｗｗ\n|  | |",
                    f"【悲報】{interaction.user.mention} さん、自分自身になりすましてしまう",
                    f"えっ待って、{interaction.user.mention} が「{interaction.user.mention}のフリ」してる……何が楽しいの？ｗｗｗ",
                    f"鏡かな？ {interaction.user.mention} が自分になりすましてるんですけどｗｗｗｗ",
                    f"自分に変装した{interaction.user.mention}、変装として一番意味がないやつで草",
                    f"哲学かな？ {interaction.user.mention} が自分自身のフリをしています",
                    f"うわぁ、{interaction.user.mention} が自分で自分になりすましてる姿、シュールすぎて笑うわ",
                    f"それただの{interaction.user.mention}じゃんｗｗｗなに一人でなりすましプレイしてんの",
                    f"自分で自分になりすますとか{interaction.user.mention} 、才能の無駄遣いすぎるｗｗｗ",
                    f"わざわざ自分になりすますとか、{interaction.user.mention} 暇すぎん…？",
                ]
            else:
                expose_msgs = [
                    f"ｷﾀ━━━━(ﾟ∀ﾟ)━━━━!!\nなりすまし犯人発見ｗｗｗｗｗｗｗｗｗ\n  ↑このメッセージ出したのコイツ → {interaction.user.mention} ｗｗｗ",
                    f"　　＿人人人人人人人＿\n　　＞  バ レ た ！ ＜\n　　￣Y^Y^Y^Y^Y^Y￣\n{interaction.user.mention} お前のなりすましバレバレやぞwwwwww 草不可避ｗｗｗｗ",
                    f"( ﾟ∀ﾟ)ｱﾊﾊ八八ﾉヽﾉヽﾉヽﾉ / \\\n{interaction.user.mention} がなりすまし失敗してて笑えるｗｗｗｗｗ\n恥ずかしくて死にたくなってそうｗｗｗ",
                    f"　∧_∧\n( ´∀｀) < {interaction.user.mention} がなりすまし！\n(　　)   こっちはとっくにバレてたｗｗｗ\n|  | |   ご苦労様でしたｗｗｗｗｗｗ",
                    f"wwwwwwwwwwwwwwwwww\n   {interaction.user.mention} の自作自演が完全にバレたｗｗｗｗｗ\nwwwwwwwwwwwwwwwwww\n哀れすぎてもう笑えないｗいや笑えるｗ",
                    f"m9(^Д^)ﾌﾟｷﾞｬｰ\n{interaction.user.mention} のなりすましダサすぎワロタｗｗｗｗｗ",
                    f"【悲報】 {interaction.user.mention} 氏、なりすましに失敗し無事死亡ｗｗｗｗｗｗ",
                    f"ﾌﾟｯ(※´Д｀※) \n{interaction.user.mention} が別人のフリしてるのバレてて草。息してる？ｗｗ",
                    f"はい、{interaction.user.mention} のなりすまし確定〜！\nみんなスクショ取れスクショ！！",
                    f"バレてやんの！ {interaction.user.mention} の自作自演だぞこれ！隠しきれると思った？ｗｗｗ",
                    f"(´・ω・`) {interaction.user.mention} のなりすまし…見てて辛いわ…",
                    f"おいおい、こいつ {interaction.user.mention} が他人のフリして喋ってるぞ！恥ずかしくないのか！ｗｗ",
                    f"正体現したね！ {interaction.user.mention} のなりすまし発言でしたー！",
                    f"うわぁ、{interaction.user.mention} がなりすましで喋ってる！見苦しいなあもう！",
                    f"ざぁぁぁこ！なりすましなんて姑息な手使って、まんまとバレてるのダサすぎでしょ！ {interaction.user.mention}",
                    f"＼(^o^)／ {interaction.user.mention} オワタ ＼(^o^)／\nなりすまし大失敗ｗｗｗｗ",
                    f"おーいみんなー！ {interaction.user.mention} がなりすまししてるぞー！指さして笑ってやれー！ｗｗｗ"
                ]
            await sent_msg.reply(random.choice(expose_msgs))

        await interaction.followup.send(
            "なりすましメッセージを送信しました。",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)

@bot.tree.command(name="impersonatechance", description="なりすましがバレる確率を設定します")
@app_commands.describe(percent="バレる確率（整数。1～100、デフォルトは10）")
async def cmd_impersonatechance(interaction: discord.Interaction, percent: int):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    if not 0 <= percent <= 100:
        await interaction.followup.send("確率は 0～100 の整数で指定してください。", ephemeral=True)
        return
    gd = db_read("impersonate", guild_id=interaction.guild_id)
    gd["expose_rate"] = percent
    db_write("impersonate", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(
        f"なりすましのバレ確率を **{percent}%** に設定しました。（0%=決してバレない・100%=必ずバレる）",
        ephemeral=True
    )

@bot.tree.command(name="impersonateset", description="なりすまし機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF",
                       channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_impersonateset(interaction: discord.Interaction,
                       scope: str = "channel", state: str = "ON",
                       channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = db_read("impersonate", guild_id=interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["server"] = on
        msg = f"サーバー全体のなりすまし機能を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["channels"] = chs
        msg = f"{target.mention} のなりすまし機能を {'ON' if on else 'OFF'} にしました。"
    db_write("impersonate", gd, guild_id=interaction.guild_id)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# 21.5 /secret 【使用注意！！】何が起こるかわかりません
# 身内鯖以外での使用は推奨しません。
# ──────────────────────────────────────────────
SECRET_WARNING = "【使用注意！！】このコマンドは何が起こるかわかりません！身内鯖以外での使用は推奨しません。"

SECRET_RICK_URL = "http://mamechosu.cloudfree.jp/dc/5655/cdn/gif/rick.gif"

SECRET_OBAMA_FALLBACK = ["おばまです", "オバマなのだ…", "…オバマ"]

SECRET_NICKNAMES = [
    "ちんちくりん", "変態さん", "おばかさん", "むしさん", "ぷにぷに星人",
    "ぺろぺろキャンディ", "ぶーぶー豚さん", "みそしるおばけ", "でろでろスライム",
    "名無しの権兵衛", "自称天才", "おこちゃま", "貧弱ちゃん", "ぽんこつロボ",
]

SECRET_CONFESS_TEMPLATES = [
    "{target}、実はずっと好きだった…付き合ってください！",
    "{target}さんのことが頭から離れません。私と付き合ってもらえませんか？",
    "ずっと言えなかったけど…{target}のことが好きです！！",
    "{target}へ。あなたに恋をしました。返事を待っています。",
    "夜も眠れないくらい{target}のことばかり考えてる…好きです。",
    "{target}、今まで隠してたけど、めちゃくちゃ好きだから付き合ってほしい。",
]

def _secret_random_member(guild: discord.Guild, exclude: set = None):
    exclude = exclude or set()
    candidates = [m for m in guild.members if not m.bot and m.id not in exclude]
    if not candidates:
        return None
    return random.choice(candidates)

async def _secret_get_webhook(channel: discord.TextChannel):
    whs = await channel.webhooks()
    wh = discord.utils.find(lambda w: w.name == "MamechosuSecret", whs)
    if not wh:
        wh = await channel.create_webhook(name="MamechosuSecret")
    return wh

async def _secret_impersonate(channel: discord.TextChannel, member: discord.Member, text: str):
    wh = await _secret_get_webhook(channel)
    avatar_url = member.display_avatar.url if member.display_avatar else member.default_avatar.url
    await wh.send(content=text, username=member.display_name, avatar_url=avatar_url)

async def _secret_past_meigen(channel: discord.TextChannel):
    candidates = []
    async for msg in channel.history(limit=300):
        if msg.author.bot or not msg.content.strip():
            continue
        c = msg.content.strip()
        if c.startswith("/") or c.startswith("http"):
            continue
        candidates.append(c)
    if not candidates:
        return None
    return random.choice(candidates)

async def _secret_fetch_avatar_bytes(member: discord.Member) -> bytes:
    try:
        if member.display_avatar:
            async with aiohttp.ClientSession() as session:
                async with session.get(member.display_avatar.url) as resp:
                    if resp.status == 200:
                        return await resp.read()
    except Exception:
        pass
    return b""

SECRET_SENRYU_FALLBACK_PARTS = [
    ["秋の空", "見上げてひとり", "ため息す"],
    ["夕焼けに", "溶けてゆく日々", "惜しみけり"],
    ["風薫る", "五月の空に", "夢のせて"],
    ["満員の", "電車の中で", "夢を見る"],
]

async def _groq_generate_senryu_parts(guild_id: int = None) -> list[str]:
    api_key = get_groq_api_key(guild_id)
    if not api_key:
        return random.choice(SECRET_SENRYU_FALLBACK_PARTS)
    prompt = (
        "あなたは川柳作家です。\n"
        "日常のおもしろい一場面を、五・七・五（17モーラ）で表現した川柳を1句だけ作ってください。\n"
        "上の句(5モーラ)・中の句(7モーラ)・下の句(5モーラ)に厳密に区切れるものだけを作ること。\n"
        "【表記の絶対ルール】\n"
        "- ひらがなだけの句は絶対に禁止。一般的な日本語の文章として、漢字を使うべき単語は必ず漢字で書くこと（例: 空、電車、夢、今日、会社 など）。\n"
        "- 助詞（は・が・を・に・で・と など）や送り仮名だけをひらがなにし、それ以外の名詞・動詞・形容詞は通常の漢字表記を用いること。\n"
        "- 全体がひらがなのみの句になっていないか、出力前に必ず確認すること。\n\n"
        "以下の形式だけで答えてください（説明不要）:\n"
        "句1|句2|句3"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 60,
                    "temperature": 0.9,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    parts = [p.strip() for p in raw.split("|")]
                    is_all_kana = all(
                        not _re_md.search(r"[\u4e00-\u9fff]", p) for p in parts
                    )
                    if len(parts) == 3 and all(parts) and not is_all_kana:
                        return parts
    except Exception:
        pass
    return random.choice(SECRET_SENRYU_FALLBACK_PARTS)

@tasks.loop(minutes=5)
async def secret_nick_revert_loop():
    now = time.time()
    for guild in bot.guilds:
        data = db_read("secret_nick", guild_id=guild.id)
        if not isinstance(data, dict) or not data:
            continue
        changed = False
        for uid_str in list(data.keys()):
            entry = data[uid_str]
            if now < entry.get("revert_at", 0):
                continue
            member = guild.get_member(int(uid_str))
            if member:
                try:
                    await member.edit(nick=entry.get("original"))
                except Exception:
                    pass
            del data[uid_str]
            changed = True
        if changed:
            db_write("secret_nick", data, guild_id=guild.id)

@bot.tree.command(
    name="secret",
    description=(
        "【使用注意！！】このコマンドは何が起こるかわかりません！身内鯖以外での使用は推奨しません。"
        "ランダムないたずらを1つだけ実行します（管理者専用）"
    )
)
@app_commands.default_permissions(administrator=True)
async def cmd_secret(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            f"{SECRET_WARNING}\nこのコマンドの実行には管理者権限が必要です。", ephemeral=True
        )
        return

    await safe_defer(interaction, ephemeral=True)
    guild = interaction.guild
    channel = interaction.channel

    event = random.choice([
        "timeout", "confess", "meigen", "senryu",
        "rickroll", "obama", "nick", "lewd",
    ])

    try:
        if event == "timeout":
            member = _secret_random_member(guild)
            if not member:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            await member.timeout(datetime.timedelta(minutes=5), reason="/secret")
            await channel.send(f"{member.mention} が5分間タイムアウトになりました…！")

        elif event == "confess":
            confesser = _secret_random_member(guild)
            if not confesser:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            target = _secret_random_member(guild, exclude={confesser.id})
            if not target:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            text = random.choice(SECRET_CONFESS_TEMPLATES).format(target=target.mention)
            await _secret_impersonate(channel, confesser, text)

        elif event == "meigen":
            member = _secret_random_member(guild)
            if not member:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            quote = await _secret_past_meigen(channel)
            if not quote:
                await interaction.followup.send("過去の発言が見つかりませんでした。", ephemeral=True); return
            avatar_bytes = await _secret_fetch_avatar_bytes(member)
            img_file = await _make_quote_file(
                quote, member.display_name, avatar_bytes,
                guild=guild, username=member.name
            )
            await channel.send(file=img_file)

        elif event == "senryu":
            member = _secret_random_member(guild)
            if not member:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            parts = await _groq_generate_senryu_parts(guild.id)
            emoji_images = await _fetch_emoji_images("".join(parts), guild=guild)
            img = build_haiku_image(parts, emoji_images=emoji_images)
            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            wh = await _secret_get_webhook(channel)
            avatar_url = member.display_avatar.url if member.display_avatar else member.default_avatar.url
            await wh.send(
                username=member.display_name, avatar_url=avatar_url,
                file=discord.File(buf, "senryu.png")
            )

        elif event == "rickroll":
            try:
                async with aiohttp.ClientSession() as rs:
                    async with rs.get(SECRET_RICK_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            raw = await resp.read()
                            await channel.send(file=discord.File(BytesIO(raw), filename="rick.gif"))
                        else:
                            await channel.send(SECRET_RICK_URL)
            except Exception:
                await channel.send(SECRET_RICK_URL)

        elif event == "obama":
            emojis = []
            obama_guild = bot.get_guild(OBAMA_GUILD_ID)
            if obama_guild:
                e = discord.utils.get(obama_guild.emojis, name="obama")
                if e: emojis.append(e)
                for i in range(1, 25):
                    e = discord.utils.get(obama_guild.emojis, name=f"obama{i}")
                    if e: emojis.append(e)
            if emojis:
                await channel.send(str(random.choice(emojis)))
            else:
                await channel.send(random.choice(SECRET_OBAMA_FALLBACK))

        elif event == "nick":
            member = _secret_random_member(guild)
            if not member:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            data = db_read("secret_nick", guild_id=guild.id)
            if not isinstance(data, dict):
                data = {}
            uid_str = str(member.id)
            original = data[uid_str]["original"] if uid_str in data else member.nick
            new_nick = random.choice(SECRET_NICKNAMES)
            await member.edit(nick=new_nick)
            data[uid_str] = {"original": original, "revert_at": time.time() + 3600}
            db_write("secret_nick", data, guild_id=guild.id)
            await channel.send(f"{member.mention} のニックネームが1時間だけ変わりました…！")

        elif event == "lewd":
            member = _secret_random_member(guild)
            if not member:
                await interaction.followup.send("対象ユーザーが見つかりませんでした。", ephemeral=True); return
            text = random.choice(LEWD_REPLIES)
            await _secret_impersonate(channel, member, text)

        await interaction.followup.send(f"実行しました。(発生した現象: {event})", ephemeral=True)
        db_log("secret_command", f"guild={guild.id} executor={interaction.user.id} event={event}")
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}", ephemeral=True)
        db_log("secret_command_error", f"guild={guild.id} | {e}", level="ERROR")

# ──────────────────────────────────────────────
# 22. AIチャット /chat (Groq + Webhook)
# ──────────────────────────────────────────────
import asyncio
import aiohttp

class CharacterSettings:
    def __init__(self, name, display_name, prompt_file, icon_file, active_time="all"):
        self.name = name
        self.display_name = display_name
        self.prompt_file = prompt_file
        self.icon_file = icon_file
        self.active_time = active_time
        self.system_prompt = ""
        self.icon_bytes = b""
        self.load()

    def load(self):
        base = os.path.dirname(os.path.abspath(__file__))
        try:
            path = os.path.join(base, self.prompt_file)
            with open(path, "r", encoding="utf-8") as f:
                self.system_prompt = f.read()
        except: pass
        try:
            path = os.path.join(base, self.icon_file)
            with open(path, "rb") as f:
                self.icon_bytes = f.read()
        except: pass

SCENARIOS = {
    "kouma": [
        CharacterSettings("ru-mia", "ルーミア", "characters/kouma/ru-mia.txt", "img/kouma/ru-mia.png", "night"),
        CharacterSettings("daiyousei", "大妖精", "characters/kouma/daiyousei.txt", "img/kouma/daiyousei.png", "day"),
        CharacterSettings("chiruno", "チルノ", "characters/kouma/chiruno.txt", "img/kouma/chiruno.png", "day"),
        CharacterSettings("meirin", "紅美鈴", "characters/kouma/meirin.txt", "img/kouma/meirin.png", "all"),
        CharacterSettings("koakuma", "小悪魔", "characters/kouma/koakuma.txt", "img/kouma/koakuma.png", "all"),
        CharacterSettings("pachuri", "パチュリー", "characters/kouma/pachuri-.txt", "img/kouma/pachuri.png", "all"),
        CharacterSettings("sakuya", "十六夜咲夜", "characters/kouma/sakuya.txt", "img/kouma/sakuya.png", "all"),
        CharacterSettings("remiria", "レミリア", "characters/kouma/remiria.txt", "img/kouma/remiria.png", "night"),
        CharacterSettings("huran", "フランドール", "characters/kouma/huran.txt", "img/kouma/huran.png", "night"),
        CharacterSettings("reimu", "博麗霊夢", "characters/kouma/reimu.txt", "img/kouma/reimu.png", "all"),
        CharacterSettings("marisa", "霧雨魔理沙", "characters/kouma/marisa.txt", "img/kouma/marisa.png", "all"),
    ]
}

def _save_active_chats():
    data = {}
    for cid, s in getattr(bot, "_active_chats", {}).items():
        data[str(cid)] = {
            "scenario_name": s.get("scenario_name", "kouma"),
            "topic": s.get("topic", "自由な雑談"),
            "history": s.get("history", [])
        }
    db_write("aichat", data, shared="index")

@tasks.loop(minutes=5)
async def task_save_active_chats():
    _save_active_chats()

if not hasattr(bot, "_active_chats"):
    bot._active_chats = {}
if not hasattr(bot, "_groq_ratelimit"):
    bot._groq_ratelimit = {"req_rem": "N/A", "req_lim": "N/A", "tok_rem": "N/A", "tok_lim": "N/A"}

async def _groq_chat_reply(char: CharacterSettings, history: list, base_topic: str, channel_id: int = None, guild_id: int = None) -> str:
    # カスタムAPIキーの決定
    api_key = get_groq_api_key(guild_id)
        
    if not api_key:
        return ""
    
    messages = [
        {"role": "system", "content": f"{char.system_prompt}\nあなたは「{char.display_name}」として振る舞ってください。話題: {base_topic}\n【重要】絵文字の使用は一切禁止です。文字のみで自然に会話してください。"}
    ]
    for msg in history[-15:]:
        if msg['name'] == char.display_name:
            role = "assistant"
            content = msg['content']
        else:
            role = "user"
            # プレフィックスなし: "名前: 発言" 形式で自然に渡す
            content = f"{msg['name']}: {msg['content']}"
        messages.append({"role": role, "content": content})
    
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    time_str = now_jst.strftime('%H時%M分')
    messages.append({
        "role": "system",
        "content": (
            f"【極秘・絶対厳守制約】\n"
            f"1. 現在の幻想郷の時刻は {time_str} です。時間帯に合わせた情緒ある反応をしてください。\n"
            "2. あなたは「東方紅魔郷」の登場人物として【完全になり着き】、AIであることを完全に忘れてください。メタ発言、解説、相槌以外の余計な言葉は一切禁止です。\n"
            "3. 話し方：キャラクター固有の【口調・語尾・一人称・二人称・独特の言い回し】を100%再現してください。一字一句、そのキャラが言いそうな言葉を選んでください。\n"
            "4. 会話形式：Discordでのリアルなチャットを再現するため、セリフは1文〜2文程度（20文字前後）に収めてください。長文は【不自然】とみなします。\n"
            "5. キャラクター性と人間味：ロボットのような無機質な返答は禁止です。性格、感情の起伏、他者への興味関心を前面に出し、まるで【本当の人間がDiscordでチャットしているかのように】振る舞ってください。「草」「www」「(?)」「...//」「！」などのネットスラングやチャット記号、AAも大歓迎ですが、絵文字（Unicode Emojiなど）の使用は【一切禁止】です。\n"
            "6. ユーザーとの対話：【ユーザーからのメッセージ（発言）をしっかりと読み、それに対して自然に反応したり、話を広げたり、ツッコミを入れたりしてください】。一方的に自分の話だけをするのはNGです。\n"
            "7. フォーマット厳守：絶対に自分の発言の先頭に「【〜の発言】」のようなプレフィックス（名前付け）を付けないでください。純粋なセリフのみを出力してください。\n"
            "8. 禁止事項：絵文字の使用、丁寧すぎる敬語（キャラ設定にない場合）、AI特有の「お手伝いしますか？」等の提案、同じフレーズの繰り返し、カギカッコの使用。\n"
            "9. 空間把握：ここは「紅魔館に関連する場所」での会話です。空気感を読み、勝手に別の世界の話題を出さないでください。"
        )
    })

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "openai/gpt-oss-120b",
                    "messages": messages,
                    "temperature": 0.9,
                    "max_tokens": 150,
                },
                timeout=15
            ) as res:
                bot._groq_ratelimit = {
                    "req_rem": res.headers.get("x-ratelimit-remaining-requests", "N/A"),
                    "req_lim": res.headers.get("x-ratelimit-limit-requests", "N/A"),
                    "tok_rem": res.headers.get("x-ratelimit-remaining-tokens", "N/A"),
                    "tok_lim": res.headers.get("x-ratelimit-limit-tokens", "N/A")
                }
                if res.status == 200:
                    data = await res.json()
                    raw = data["choices"][0]["message"]["content"].strip()
                    # 「」で囲まれていたら外す
                    if raw.startswith("「") and raw.endswith("」"):
                        raw = raw[1:-1]
                    # 【〇〇の発言】【〇〇】などのプレフィックスを除去
                    import re as _re
                    raw = _re.sub(r'^[【【][^】】]{0,30}[】】][:\s：]*', '', raw).strip()
                    raw = _re.sub(r'^[\w\u30a0-\u30ff\u3040-\u309f\u4e00-\u9fff]{1,20}[:\s：]+', '', raw).strip()
                    return raw
                else:
                    err_text = await res.text()
                    if channel_id:
                        ch = bot.get_channel(channel_id)
                        if ch: await ch.send(f"[警告] Groq API Error ({res.status}): `{err_text[:100]}`", delete_after=10)
    except Exception as e:
        if channel_id:
            ch = bot.get_channel(channel_id)
            if ch: await ch.send(f"[警告] Chat Exception: `{str(e)[:100]}`", delete_after=10)
    return ""

async def _chat_loop(channel_id: int):
    session = bot._active_chats.get(channel_id)
    if not session: return

    channel = bot.get_channel(channel_id)
    if not channel: return
    
    webhook = None
    try:
        webhooks = await channel.webhooks()
        webhook = next((w for w in webhooks if w.name == "MamechosuChat"), None)
        if not webhook:
            webhook = await channel.create_webhook(name="MamechosuChat")
    except Exception as e:
        pass
        return

    history = session["history"]
    context_topic = session.get("topic", "自由な雑談")
    chars = session["chars"]

    while bot._active_chats.get(channel_id) == session:
        # ランダムな待機時間（呼吸や自然な間を生む 1~3秒）
        await asyncio.sleep(random.uniform(1.0, 3.0))
        
        # 誰かが話す (時間の生態を加味して抽選)
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        is_day = 6 <= now_jst.hour < 18
        
        weights = []
        for c in chars:
            if c.active_time == "all": weights.append(10)
            elif c.active_time == "day" and is_day: weights.append(15)
            elif c.active_time == "night" and not is_day: weights.append(15)
            else: weights.append(1)
        
        speaker = random.choices(chars, weights=weights, k=1)[0]
        
        reply = await _groq_chat_reply(speaker, history, context_topic, channel_id=channel_id, guild_id=channel.guild.id)
        if reply:
            history.append({"name": speaker.display_name, "content": reply})
            if len(history) > 20: history.pop(0)

            try:
                if speaker.icon_bytes:
                    await webhook.edit(name=speaker.display_name, avatar=speaker.icon_bytes)
                else:
                    await webhook.edit(name=speaker.display_name, avatar=None)
                await webhook.send(content=reply)
            except Exception as e:
                pass

        # 会話頻度ロジック（10〜20分間隔などの長期待機）
        guild_id = channel.guild.id
        settings = db_read("aichat_settings", str(guild_id))
        if not isinstance(settings, dict): settings = {}
        interval_min = int(settings.get("interval_min", 10))
        interval_max = int(settings.get("interval_max", 20))
        wait_seconds = random.uniform(interval_min * 60.0, interval_max * 60.0)
        await asyncio.sleep(wait_seconds)

@bot.tree.command(name="apikey", description="サーバー独自のGroq APIキーを設定します（管理者専用・川柳/えっち検出/AIチャット等すべてのAI機能に適用されます）")
@app_commands.describe(api_key="設定するGroq APIキー（空の場合は削除）")
@app_commands.default_permissions(manage_guild=True)
async def cmd_apikey(interaction: discord.Interaction, api_key: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("サーバー管理権限が必要です。", ephemeral=True)
        return
        
    gid = interaction.guild_id
    settings = db_read("aichat_settings", str(gid))
    if not isinstance(settings, dict): settings = {}
    
    if api_key:
        settings["custom_api_key"] = api_key
        db_write("aichat_settings", settings, guild_id=gid)
        await interaction.followup.send("サーバー独自のAPIキーを保存しました。このサーバーのAI機能（川柳検出・えっち検出・熱盛検知・ローマ字翻訳・meigen・sakubun・AIチャット等）すべてに適用されます。", ephemeral=True)
    else:
        if "custom_api_key" in settings:
            del settings["custom_api_key"]
            db_write("aichat_settings", settings, guild_id=gid)
            await interaction.followup.send("カスタムAPIキーを削除し、デフォルトに戻しました。", ephemeral=True)
        else:
            await interaction.followup.send("カスタムAPIキーは設定されていません。", ephemeral=True)

@bot.tree.command(name="chat", description="AIキャラクターの自律会話を開始・停止します")
@app_commands.describe(action="start / stop", scenario="参加キャラクターのシナリオ", topic="会話の話題(任意)", interval_min="会話の最小間隔(分)", interval_max="会話の最大間隔(分)")
@app_commands.choices(action=[
    app_commands.Choice(name="開始 (start)", value="start"),
    app_commands.Choice(name="停止 (stop)", value="stop")
], scenario=[
    app_commands.Choice(name="紅魔郷", value="kouma")
])
async def cmd_chat(interaction: discord.Interaction, action: app_commands.Choice[str], scenario: app_commands.Choice[str], topic: str = "自由な雑談", interval_min: int = None, interval_max: int = None):
    await safe_defer(interaction)
    cid = interaction.channel_id
    act_val = action.value
    scn_val = scenario.value
    
    if act_val == "start":
        if cid in bot._active_chats:
            await interaction.followup.send("すでにこのチャンネルでチャットが進行中です。", ephemeral=True)
            return
            
        # 頻度の設定
        if interval_min is not None or interval_max is not None:
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.followup.send("頻度の変更にはサーバー管理権限が必要です。", ephemeral=True)
                return
            vmin = interval_min if interval_min is not None else 10
            vmax = interval_max if interval_max is not None else 20
            if vmin < 1 or vmax < 1 or vmin > vmax:
                await interaction.followup.send("正しい頻度を入力してください(最小<=最大)。", ephemeral=True)
                return
            gid = interaction.guild_id
            settings = db_read("aichat_settings", str(gid))
            if not isinstance(settings, dict): settings = {}
            settings["interval_min"] = vmin
            settings["interval_max"] = vmax
            db_write("aichat_settings", settings, guild_id=gid)
        
        chars = SCENARIOS.get(scn_val)
        if not chars:
            await interaction.followup.send(f"未知のシナリオです", ephemeral=True)
            return
        
        chars = SCENARIOS[scn_val]
        bot._active_chats[cid] = {
            "chars": chars,
            "scenario_name": scn_val,
            "topic": topic,
            "history": [],
            "task": None
        }
        await interaction.followup.send(f"新しい会話を開始しました！ (シナリオ: **{scenario.name}**, 話題: **{topic}**)\n※停止する場合は `/chat action:stop` と入力してください。")
        
        # 開始メッセージを埋め込む
        bot._active_chats[cid]["history"].append({"name": "System", "content": f"新しい話題「{topic}」について会話を開始しました。"})
        
        task = asyncio.create_task(_chat_loop(cid))
        bot._active_chats[cid]["task"] = task
        _save_active_chats()
        
    elif act_val == "stop":
        session = bot._active_chats.pop(cid, None)
        if session:
            try:
                session["task"].cancel()
            except: pass
            _save_active_chats()
            try:
                ch = bot.get_channel(cid)
                if ch:
                    for wh in await ch.webhooks():
                        if wh.name == "MamechosuChat":
                            await wh.delete()
                            break
            except Exception:
                pass
            await interaction.followup.send("会話を終了しました。")
        else:
            await interaction.followup.send("このチャンネルで進行中の会話はありません。", ephemeral=True)
            
    else:
        await interaction.followup.send("actionには start または stop を指定してください。", ephemeral=True)

# ──────────────────────────────────────────────
# Bot 起動
# ──────────────────────────────────────────────
import os as _os
if not _os.environ.get("DEPLOY_MODE"):
    bot.run(TOKEN, log_handler=None)
