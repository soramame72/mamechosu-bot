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

# ──────────────────────────────────────────────
# データ管理 (date.txt は JSON)
# ──────────────────────────────────────────────
DATA_FILE    = "date.txt"
DATA_BACKUP  = "date.bak.txt"

import threading as _threading
_data_lock = _threading.Lock()

def load_data() -> dict:
    with _data_lock:
        for path in [DATA_FILE, DATA_BACKUP]:
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
        return {}

def save_data(data: dict):
    with _data_lock:
        # バックアップを作成してからメインを上書き
        try:
            if os.path.exists(DATA_FILE):
                import shutil as _shutil
                _shutil.copy2(DATA_FILE, DATA_BACKUP)
        except Exception:
            pass
        try:
            with open(DATA_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(DATA_FILE + ".tmp", DATA_FILE)
        except Exception as e:
            print(f"[data] 保存エラー: {e}")

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

def get_guild_data(guild_id: int) -> dict:
    data = load_data()
    gid = str(guild_id)
    return data.get(gid, {})

def set_guild_data(guild_id: int, guild_data: dict):
    data = load_data()
    data[str(guild_id)] = guild_data
    save_data(data)

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
# エラーハンドラ (全コマンド共通)
# ──────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # 内部情報を外部に漏らさないようにユーザー向けメッセージを簡略化
    if isinstance(error, app_commands.MissingPermissions):
        msg = "権限が不足しています。"
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"クールダウン中です。{error.retry_after:.1f}秒後に再試行してください。"
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "このコマンドはサーバー内でのみ使用できます。"
    else:
        msg = "コマンドの実行中にエラーが発生しました。"
    # 詳細はコンソールに出力
    print(f"[cmd_error] {type(error).__name__}: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
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
# on_ready
# ──────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"ログイン: {bot.user} (ID: {bot.user.id})")
    try:
        h_cmd = bot.tree.get_command("h")
        if h_cmd:
            h_cmd.nsfw = True
        synced = await bot.tree.sync()
        print(f"{len(synced)} コマンド同期完了")
    except Exception as e:
        print(f"コマンド同期失敗: {e}")
    # ステータスを ver1.1 固定で設定
    await bot.change_presence(
        activity=discord.CustomActivity(name="ver1.1"))


# ──────────────────────────────────────────────
# 2. コマンド一覧 / ヘルプ
# ──────────────────────────────────────────────
@bot.tree.command(name="commands", description="コマンド一覧を表示します")
async def cmd_list(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    cmds = bot.tree.get_commands()
    desc = "\n".join(f"/{c.name} — {c.description}" for c in cmds)
    embed = discord.Embed(title="コマンド一覧", description=desc, color=0x5865F2)
    await interaction.followup.send(embed=embed)

HELP_TEXT = {
    "commands":   "コマンド一覧を表示します。",
    "help":       "/help [コマンド名] で詳細表示。",
    "cp":         "コントロールパネルをGUIで開きます。（3ページ構成）",
    "rolepanel":  "ロールパネルを作成します。パスワード・複数ボタン対応。\n例: /rolepanel roles:@A,@B title:ロール選択",
    "welcome":    "歓迎メッセージ設定。{user}=メンション, {members}=人数。\n例: /welcome set #general ようこそ{user}さん！",
    "goodbye":    "送別メッセージ設定。{user}=名前, {members}=人数。",
    "wordblock":  "禁止ワード管理。\naction: add=追加 / remove=削除(選択UI) / list=一覧",
    "verify":     "認証パネル作成。レベル1〜10で保護強度を設定。\n例: /verify level:3 role:@認証済み",
    "autoreply":  "自動返信設定。\naction: add=追加 / remove=削除 / list=一覧",
    "reaction":   "指定メッセージにobama絵文字25個をランダムでつけます。",
    "haiku":      "川柳検出ON/OFF。\nscope: channel=このチャンネル / server=サーバー全体\nstate: ON / OFF",
    "resource":   "CPU/メモリ/ストレージ/Ping等のリソース状態を表示します。",
    "save":       "サーバーのロール・チャンネル・権限をバックアップします。\n共有コードで他サーバーでも使用可能。",
    "restore":    "バックアップからサーバーを復元します。\nオプション: code=他サーバーのコード",
    "lewd":       "えっち検出ON/OFF。\nscope: channel / server  state: ON / OFF",
    "atsumori":   "熱盛検知ON/OFF。スポーツ等の熱い場面を検知してatsumori.pngを送信。\nscope: channel / server  state: ON / OFF",
    "h":          "NSFWチャンネル限定: えっちな画像をランダムで取得します。",
    "globalchat": "グローバルチャット管理。\naction: join=参加 / leave=退出 / list=一覧",
    "permission": "Botの権限と状態を一覧表示します。",
    "quote":      "名言カード画像を生成します。\n例: /quote text:夢を諦めるな author:無名 theme:dark",
    "purge":      "直近N件のメッセージを削除します（最大100件）。\n例: /purge count:50",
    "grok":       "grok_dc の GitHub リポジトリリンクを表示します。",
    "supiki":     "ｽﾋﾟｷになります。",
}

@bot.tree.command(name="help", description="各コマンドの使い方を表示します")
@app_commands.describe(command="調べたいコマンド名（省略すると全体）")
async def cmd_help(interaction: discord.Interaction, command: str = None):
    await safe_defer(interaction, ephemeral=True)
    if command and command in HELP_TEXT:
        embed = discord.Embed(title=f"/{command}", description=HELP_TEXT[command], color=0x57F287)
    else:
        embed = discord.Embed(title="ヘルプ", color=0x57F287)
        for k, v in HELP_TEXT.items():
            embed.add_field(name=f"/{k}", value=v.split("\n")[0], inline=False)
        embed.set_footer(text="/help [コマンド名] で詳細を表示")
    await interaction.followup.send(embed=embed)

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
            gd  = get_guild_data(self.gid)
            gd["welcome_channel"] = cid; gd["welcome_message"] = self.msg.value
            set_guild_data(self.gid, gd)
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
            gd  = get_guild_data(self.gid)
            gd["goodbye_channel"] = cid; gd["goodbye_message"] = self.msg.value
            set_guild_data(self.gid, gd)
            ch = interaction.guild.get_channel(cid)
            await interaction.response.send_message(
                f"送別メッセージを設定しました → {ch.mention if ch else cid}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class WordAddModal(discord.ui.Modal, title="禁止ワード追加"):
    word = discord.ui.TextInput(label="追加するワード")
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        gd = get_guild_data(self.gid)
        bl = gd.get("blocked_words", [])
        w  = self.word.value.strip()
        if w and w not in bl: bl.append(w)
        gd["blocked_words"] = bl; set_guild_data(self.gid, gd)
        await interaction.response.send_message(f"`{w}` を追加しました。", ephemeral=True)

class AutoreplyAddModal(discord.ui.Modal, title="自動返信追加"):
    trigger = discord.ui.TextInput(label="トリガーワード")
    reply   = discord.ui.TextInput(label="返信テキスト", style=discord.TextStyle.paragraph)
    emoji   = discord.ui.TextInput(label="リアクション絵文字（省略可）", required=False)
    def __init__(self, guild_id): super().__init__(); self.gid = guild_id
    async def on_submit(self, interaction):
        gd = get_guild_data(self.gid)
        ar = gd.get("autoreplies", {})
        ar[self.trigger.value] = {"text": self.reply.value, "emoji": self.emoji.value or ""}
        gd["autoreplies"] = ar; set_guild_data(self.gid, gd)
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

class RolePanelModal(discord.ui.Modal, title="ロールパネル作成"):
    roles_input = discord.ui.TextInput(label="ロールIDをカンマ区切りで入力",
                                        placeholder="123456789, 987654321")
    panel_title = discord.ui.TextInput(label="パネルタイトル", default="ロールパネル")
    password    = discord.ui.TextInput(label="パスワード（省略可）", required=False)
    def __init__(self, guild_id, channel_id): super().__init__(); self.gid = guild_id; self.cid = channel_id
    async def on_submit(self, interaction):
        try:
            ch    = interaction.guild.get_channel(self.cid)
            ids   = [s.strip() for s in self.roles_input.value.split(",") if s.strip()]
            roles = [interaction.guild.get_role(int(r)) for r in ids if r.isdigit()]
            roles = [r for r in roles if r]
            if not roles:
                await interaction.response.send_message("有効なロールが見つかりません。", ephemeral=True); return
            pw    = self.password.value.strip() or None
            embed = discord.Embed(title=self.panel_title.value,
                description="ボタンでロールを取得/解除できます。", color=0x5865F2)
            if pw: embed.set_footer(text="このパネルはパスワード保護されています")
            await ch.send(embed=embed, view=RolePanelView(roles, pw))
            await interaction.response.send_message("ロールパネルを作成しました。", ephemeral=True)
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
        gd = get_guild_data(self.guild_id)
        if self.scope == "server":
            gd[f"{self.feature}_server"] = self.on
        else:
            chs = gd.get(f"{self.feature}_channels", [])
            if self.on and interaction.channel_id not in chs: chs.append(interaction.channel_id)
            elif not self.on and interaction.channel_id in chs: chs.remove(interaction.channel_id)
            gd[f"{self.feature}_channels"] = chs
        set_guild_data(self.guild_id, gd)
        scope_txt = "サーバー全体" if self.scope == "server" else "このチャンネル"
        if self.feature == "haiku":
            feat_txt = "川柳検出"
        elif self.feature == "atsumori":
            feat_txt = "熱盛検知"
        else:
            feat_txt = "えっち検出"
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
        gd = get_guild_data(self.gid)
        words = gd.get("blocked_words", [])
        text = "\n".join(f"• {w}" for w in words) or "なし"
        await i.response.send_message(f"禁止ワード:\n{text}", ephemeral=True)
class _BtnAddAutoreply(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="自動返信追加", style=discord.ButtonStyle.success, row=2); self.gid=gid
    async def callback(self, i): await i.response.send_modal(AutoreplyAddModal(self.gid))

class _BtnListAutoreply(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="自動返信一覧", style=discord.ButtonStyle.secondary, row=2); self.gid=gid
    async def callback(self, i):
        gd = get_guild_data(self.gid)
        ar = gd.get("autoreplies", {})
        text = "\n".join(f"• `{k}` → {v['text']}" for k,v in ar.items()) or "なし"
        await i.response.send_message(f"自動返信:\n{text}", ephemeral=True)

class _BtnPreviewWelcome(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="歓迎メッセージ プレビュー", style=discord.ButtonStyle.secondary, row=3); self.gid=gid
    async def callback(self, i):
        gd  = get_guild_data(self.gid)
        msg = gd.get("welcome_message", "未設定")
        pre = msg.replace("{user}", i.user.mention).replace("{members}", str(i.guild.member_count))
        await i.response.send_message(f"**プレビュー:**\n{pre}", ephemeral=True)

class _BtnPreviewGoodbye(discord.ui.Button):
    def __init__(self, gid): super().__init__(label="送別メッセージ プレビュー", style=discord.ButtonStyle.secondary, row=3); self.gid=gid
    async def callback(self, i):
        gd  = get_guild_data(self.gid)
        msg = gd.get("goodbye_message", "未設定")
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
        gd = get_guild_data(self.gid); bk = gd.get("backup")
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
        gd = get_guild_data(self.gid)
        wch = gd.get("welcome_channel"); fch = get_guild_data(self.gid).get("goodbye_channel")
        lines = [
            f"歓迎ch: {i.guild.get_channel(wch).mention if wch and i.guild.get_channel(wch) else '未設定'}",
            f"送別ch: {i.guild.get_channel(fch).mention if fch and i.guild.get_channel(fch) else '未設定'}",
            f"禁止ワード: {len(gd.get('blocked_words',[]))}件",
            f"自動返信: {len(gd.get('autoreplies',{}))}件",
            f"川柳検出ch: {len(gd.get('haiku_channels',[]))}件" + (" +全体" if gd.get("haiku_server") else ""),
            f"えっち検出ch: {len(gd.get('lewd_channels',[]))}件" + (" +全体" if gd.get("lewd_server") else ""),
        ]
        embed = discord.Embed(title="現在の設定一覧", description="\n".join(lines), color=0x5865F2)
        await i.response.send_message(embed=embed, ephemeral=True)

class _BtnCreateVerify(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="認証パネル作成", style=discord.ButtonStyle.success, row=2); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(VerifySetModal(self.gid, self.cid))

class _BtnCreateRolePanel(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="ロールパネル作成", style=discord.ButtonStyle.success, row=2); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(RolePanelModal(self.gid, self.cid))

class _BtnGlobalChat(discord.ui.Button):
    def __init__(self, gid, cid): super().__init__(label="グローバルチャット", style=discord.ButtonStyle.primary, row=3); self.gid=gid; self.cid=cid
    async def callback(self, i): await i.response.send_modal(GlobalChatModal(self.gid, self.cid))

class _BtnPurge(discord.ui.Button):
    def __init__(self, ch): super().__init__(label="メッセージ一括削除", style=discord.ButtonStyle.danger, row=3); self.ch=ch
    async def callback(self, i):
        if not i.user.guild_permissions.manage_messages:
            await i.response.send_message("メッセージ管理権限が必要です。", ephemeral=True); return
        await i.response.send_modal(PurgeModal(self.ch))

# ── CPView (5ページ構成) ──────────────────────
class CPView(discord.ui.View):
    PAGE_TITLES = [
        "メッセージ・ワード管理",
        "川柳 ON/OFF",
        "えっち検出 ON/OFF",
        "熱盛検知 ON/OFF",
        "サーバー情報・バックアップ / パネル作成",
    ]
    MAX_PAGE = 4

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
            # ページ5: サーバー情報・バックアップ / パネル作成
            self.add_item(_BtnResource());         self.add_item(_BtnPermission())
            self.add_item(_BtnBackup(gid));        self.add_item(_BtnSettings(gid))
            ch = bot.get_channel(cid)
            self.add_item(_BtnCreateVerify(gid, cid))
            self.add_item(_BtnCreateRolePanel(gid, cid))
            self.add_item(_BtnGlobalChat(gid, cid))
            if ch:
                self.add_item(_BtnPurge(ch))

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

@bot.tree.command(name="cp", description="コントロールパネルを開きます")
async def cmd_cp(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    view  = CPView(interaction.guild_id, interaction.channel_id)
    embed = view._make_embed()
    await interaction.followup.send(embed=embed, view=view)



# ──────────────────────────────────────────────
# 5. ロールパネル
# ──────────────────────────────────────────────
class RoleButton(discord.ui.Button):
    def __init__(self, role: discord.Role, password: str = None):
        super().__init__(label=role.name, style=discord.ButtonStyle.success, custom_id=f"role_{role.id}")
        self.role_id  = role.id
        self.password = password

    async def callback(self, interaction: discord.Interaction):
        if self.password:
            await interaction.response.send_modal(PasswordModal(self.role_id, self.password))
        else:
            await _toggle_role(interaction, self.role_id)

class PasswordModal(discord.ui.Modal, title="パスワード入力"):
    pw = discord.ui.TextInput(label="パスワード", required=True)
    def __init__(self, role_id: int, correct_pw: str):
        super().__init__()
        self.role_id    = role_id
        self.correct_pw = correct_pw
    async def on_submit(self, interaction: discord.Interaction):
        # 試行回数チェック (3回失敗で60秒ロック)
        if not _check_password_attempt(interaction.user.id, self.role_id):
            await interaction.response.send_message(
                "試行回数が多すぎます。60秒後に再試行してください。", ephemeral=True)
            return
        if self.pw.value == self.correct_pw:
            _clear_password_attempt(interaction.user.id, self.role_id)
            await _toggle_role(interaction, self.role_id)
        else:
            await interaction.response.send_message(
                "パスワードが違います。", ephemeral=True)

async def _toggle_role(interaction: discord.Interaction, role_id: int):
    role = interaction.guild.get_role(role_id)
    if not role:
        await interaction.response.send_message("ロールが見つかりません。", ephemeral=True)
        return
    # Botのロールより上位かチェック
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(
            f"Botのロール ({interaction.guild.me.top_role.name}) より上位のロールは操作できません。", ephemeral=True)
        return
    member = interaction.user
    try:
        if role in member.roles:
            await member.remove_roles(role)
            await interaction.response.send_message(f"{role.name} を外しました。", ephemeral=True)
        else:
            await member.add_roles(role)
            await interaction.response.send_message(f"{role.name} を付与しました。", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "権限エラー: BotのロールをDiscordの設定でより上位に移動してください。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"エラー: {e}", ephemeral=True)

class RolePanelView(discord.ui.View):
    def __init__(self, roles: list, password: str = None):
        super().__init__(timeout=None)
        for role in roles:
            self.add_item(RoleButton(role, password))

@bot.tree.command(name="rolepanel", description="ロールパネルを作成します")
@app_commands.describe(roles="ロールをメンション形式でカンマ区切り", title="タイトル", password="パスワード（省略可）")
async def cmd_rolepanel(interaction: discord.Interaction, roles: str, title: str = "ロールパネル", password: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.followup.send("ロール管理権限が必要です。", ephemeral=True)
        return
    role_list = []
    for part in roles.split(","):
        m = re.search(r"<@&(\d+)>", part.strip())
        if m:
            r = interaction.guild.get_role(int(m.group(1)))
            if r:
                role_list.append(r)
    if not role_list:
        await interaction.followup.send("ロールが見つかりませんでした。", ephemeral=True)
        return
    embed = discord.Embed(title=f"{title}", description="ボタンでロールを取得/解除できます。", color=0x5865F2)
    if password:
        embed.set_footer(text="このパネルはパスワード保護されています")
    await interaction.channel.send(embed=embed, view=RolePanelView(role_list, password))
    await interaction.followup.send("ロールパネルを作成しました。", ephemeral=True)

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
    gd = get_guild_data(interaction.guild_id)
    if action == "set":
        if not channel or not message:
            await interaction.followup.send("チャンネルとメッセージを指定してください。", ephemeral=True)
            return
        gd["welcome_channel"] = channel.id
        gd["welcome_message"] = message
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send(f"歓迎メッセージを設定しました → {channel.mention}", ephemeral=True)
    elif action == "preview":
        msg = gd.get("welcome_message", "未設定")
        preview = msg.replace("{user}", interaction.user.mention).replace("{members}", str(interaction.guild.member_count))
        await interaction.followup.send(f"プレビュー:\n{preview}", ephemeral=True)
    elif action == "off":
        gd.pop("welcome_channel", None); gd.pop("welcome_message", None)
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send("歓迎メッセージを無効化しました。", ephemeral=True)

@bot.tree.command(name="goodbye", description="送別メッセージを設定します")
@app_commands.describe(action="set / preview / off", channel="送信先チャンネル", message="{user}/{members}が使えます")
async def cmd_goodbye(interaction: discord.Interaction, action: str, channel: discord.TextChannel = None, message: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("サーバー管理権限が必要です。", ephemeral=True)
        return
    gd = get_guild_data(interaction.guild_id)
    if action == "set":
        if not channel or not message:
            await interaction.followup.send("チャンネルとメッセージを指定してください。", ephemeral=True)
            return
        gd["goodbye_channel"] = channel.id
        gd["goodbye_message"] = message
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send(f"送別メッセージを設定しました → {channel.mention}", ephemeral=True)
    elif action == "preview":
        msg = gd.get("goodbye_message", "未設定")
        preview = msg.replace("{user}", interaction.user.display_name).replace("{members}", str(interaction.guild.member_count))
        await interaction.followup.send(f"プレビュー:\n{preview}", ephemeral=True)
    elif action == "off":
        gd.pop("goodbye_channel", None); gd.pop("goodbye_message", None)
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send("送別メッセージを無効化しました。", ephemeral=True)

@bot.event
async def on_member_join(member: discord.Member):
    gd = get_guild_data(member.guild.id)
    ch_id = gd.get("welcome_channel")
    msg   = gd.get("welcome_message")
    if ch_id and msg:
        ch = member.guild.get_channel(ch_id)
        if ch:
            await ch.send(msg.replace("{user}", member.mention).replace("{members}", str(member.guild.member_count)))

@bot.event
async def on_member_remove(member: discord.Member):
    gd = get_guild_data(member.guild.id)
    ch_id = gd.get("goodbye_channel")
    msg   = gd.get("goodbye_message")
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
        gd = get_guild_data(self.guild_id)
        blocked = gd.get("blocked_words", [])
        choice = self.values[0]
        if choice == "__all__":
            gd["blocked_words"] = []
            msg = "禁止ワードを全て削除しました。"
        else:
            if choice in blocked:
                blocked.remove(choice)
            gd["blocked_words"] = blocked
            msg = f"`{choice}` を削除しました。"
        set_guild_data(self.guild_id, gd)
        await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="wordblock", description="禁止ワード/絵文字を管理します")
@app_commands.describe(action="add / remove / list", word="追加するワード（removeは省略可）")
async def cmd_wordblock(interaction: discord.Interaction, action: str, word: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_messages:
        await interaction.followup.send("メッセージ管理権限が必要です。", ephemeral=True)
        return
    gd = get_guild_data(interaction.guild_id)
    blocked = gd.get("blocked_words", [])
    if action == "add":
        if not word:
            await interaction.followup.send("ワードを指定してください。", ephemeral=True)
            return
        if word not in blocked:
            blocked.append(word)
        gd["blocked_words"] = blocked
        set_guild_data(interaction.guild_id, gd)
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
# on_message (禁止ワード / 自動返信 / 川柳 / えっち / グローバルチャット)
# ──────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await bot.process_commands(message)
    # DM受信時 → えっち語録をランダム返信
    if not message.guild:
        if message.content.strip():
            await message.channel.send(random.choice(LEWD_REPLIES))
        return
    gd = get_guild_data(message.guild.id)

    # 禁止ワード (大文字小文字無視・単語境界考慮)
    content_lower = message.content.lower()
    for word in gd.get("blocked_words", []):
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

    # 自動返信 (クールダウン3秒・完全一致or部分一致を設定で選べる)
    if not _check_rate(f"autoreply:{message.channel.id}", cooldown_sec=3.0):
        pass  # クールダウン中はスキップ
    else:
        for trigger, rd in gd.get("autoreplies", {}).items():
            # 完全一致モード or 部分一致（デフォルト部分一致）
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

    # 川柳検出
    if gd.get("haiku_server") or message.channel.id in gd.get("haiku_channels", []):
        await check_haiku(message)

    # 熱盛検知
    if gd.get("atsumori_server") or message.channel.id in gd.get("atsumori_channels", []):
        await check_atsumori(message)

    # えっち検出
    if gd.get("lewd_server") or message.channel.id in gd.get("lewd_channels", []):
        await check_lewd(message)

    # グローバルチャット
    await relay_global_message(message)



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
    def __init__(self, role_id, question, answer):
        super().__init__(); self.role_id = role_id; self.answer = answer
        self.answer_input.label = question
    async def on_submit(self, interaction):
        if self.answer_input.value.strip() == self.answer:
            await _grant_role(interaction, self.role_id)
        else:
            await interaction.response.send_message("答えが違います。", ephemeral=True)

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
    gd = get_guild_data(interaction.guild_id)
    autoreplies = gd.get("autoreplies", {})
    if action == "add":
        if not trigger or not reply:
            await interaction.followup.send("トリガーと返信テキストを指定してください。", ephemeral=True)
            return
        autoreplies[trigger] = {"text": reply, "emoji": emoji or ""}
        gd["autoreplies"] = autoreplies
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send(f"自動返信を追加: `{trigger}`", ephemeral=True)
    elif action == "remove":
        autoreplies.pop(trigger or "", None)
        gd["autoreplies"] = autoreplies
        set_guild_data(interaction.guild_id, gd)
        await interaction.followup.send(f"`{trigger}` の自動返信を削除しました。", ephemeral=True)
    elif action == "list":
        text = "\n".join(f"`{k}` → {v['text']}" for k, v in autoreplies.items()) or "なし"
        await interaction.followup.send(f"自動返信一覧:\n{text}", ephemeral=True)

# ──────────────────────────────────────────────
# 10. リアクション /reaction
# ──────────────────────────────────────────────
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

def split_into_phrases(text: str) -> list[str] | None:
    """
    川柳の3フレーズを検出する。
    字余り・字足らずも許容する。
    - 区切り文字があれば3分割を試みる
    - なければ5-7-5±1モーラの範囲で全探索
    """
    stripped = text.strip()
    if stripped.startswith("http"):
        return None
    if len(stripped) > 60 or len(stripped) < 5:
        return None

    # 1) 区切り文字で3分割できる場合
    parts = re.split(r"[\s　、。,.・/\n！!？?～~]+", stripped)
    parts = [p for p in parts if p]
    if len(parts) == 3:
        # 各フレーズが2〜9モーラなら川柳として扱う（字余り・字足らず許容）
        moras = [count_mora(p) for p in parts]
        if all(2 <= m <= 9 for m in moras):
            return parts

    # 2) 区切りなし: 4〜6 / 5〜9 / 4〜6 の範囲で全探索（緩い制約）
    clean = re.sub(r"[\s　、。,.・/\n！!？?～~]", "", stripped)
    n     = len(clean)
    total = count_mora(clean)
    # 合計モーラが11〜21の範囲にあるものだけ対象
    if not (11 <= total <= 21):
        return None
    for i in range(2, n-2):
        m1 = count_mora(clean[:i])
        if not (4 <= m1 <= 6):
            continue
        for j in range(i+2, n):
            m2 = count_mora(clean[i:j])
            if m2 > 9:
                break
            if 5 <= m2 <= 9:
                m3 = count_mora(clean[j:])
                if 4 <= m3 <= 6:
                    return [clean[:i], clean[i:j], clean[j:]]
    return None

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

def build_haiku_image(parts: list[str]) -> Image.Image:
    """
    縦書き・和紙風俳句カード。W=380 H=560 固定。
    最長句の文字数でフォントサイズ・char_hを動的計算し枠内に必ず収める。
    """
    import random as _rnd

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

    # 最長句の文字数からchar_h・font_sizeを動的決定
    max_len   = max(len(p) for p in parts) if parts else 7
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

    # 縦書き3列
    for col_idx, phrase in enumerate(parts):
        cx = col_xs[col_idx]
        y  = TOP_Y
        for ch_char in phrase:
            if y + font_size > H - BOT_PAD:  # 枠内に収まらなければ停止
                break
            draw.text((cx, y), ch_char, font=f_main, fill=INK, anchor="mt")
            y += char_h

    return img

async def _groq_extract_haiku(text: str) -> list[str] | None:
    """
    GROQを使って文章中の川柳を検出する。
    5-7-5に限定せず、字余り・字足らずも許容する。
    """
    if not GROQ_API_KEY:
        return None
    try:
        prompt = (
            "あなたは川柳・俳句の専門家です。\n"
            "次の【元の文章】を見て、川柳・俳句として3フレーズに区切れるか判断してください。\n"
            f"【元の文章】: 「{text}」\n\n"
            "【絶対ルール】\n"
            "- 出力する句1・句2・句3は【元の文章】に含まれる文字だけを使うこと\n"
            "- 元の文章にない言葉を追加・変更・創作することは禁止\n"
            "- 元の文章をそのまま3分割するだけでよい\n\n"
            "【判断基準】\n"
            "- 上の句(5モーラ前後) / 中の句(7モーラ前後) / 下の句(5モーラ前後)に自然に区切れるか\n"
            "- 字余り・字足らずは許容する（±2モーラまでOK）\n"
            "- 日常会話の一文でも川柳のリズムがあればOK\n"
            "- 区切りは言葉の意味・リズム・息継ぎで自然に決める\n\n"
            "川柳として区切れる場合、以下の形式だけで答えてください（説明不要）:\n"
            "句1|句2|句3\n\n"
            "例（5-7-5）: 元「古池や蛙飛び込む水の音」→ 古池や|蛙飛び込む|水の音\n"
            "例（字余り）: 元「財布忘れたよ電車の中で気づいた」→ 財布忘れたよ|電車の中で|気づいた\n\n"
            "川柳のリズムが感じられない文章は「なし」とだけ答えてください。"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
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
                    if joined not in clean_text and clean_text not in joined:
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
        if GROQ_API_KEY and 5 <= len(text) <= 120:
            parts = await _groq_extract_haiku(text)
        # フォールバック: ローカル検出
        if parts is None:
            parts = split_into_phrases(text)
        if parts:
            img = build_haiku_image(parts)
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
    gd = get_guild_data(interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["haiku_server"] = on
        msg = f"サーバー全体の川柳検出を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("haiku_channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["haiku_channels"] = chs
        msg = f"{target.mention} の川柳検出を {'ON' if on else 'OFF'} にしました。"
    set_guild_data(interaction.guild_id, gd)
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
    dsz    = os.path.getsize(DATA_FILE) / 1024 if os.path.exists(DATA_FILE) else 0

    embed = discord.Embed(title="リソースモニター", color=_color_from_pct(max(cpu, mem.percent, disk.percent)),
                          timestamp=datetime.datetime.utcnow())
    embed.add_field(name="CPU",        value=f"`{_bar(cpu)}` {cpu:.1f}%", inline=False)
    embed.add_field(name="メモリ",      value=f"`{_bar(mem.percent)}` {mem.percent:.1f}%  ({mem.used//1024//1024:,}MB / {mem.total//1024//1024:,}MB)", inline=False)
    embed.add_field(name="ストレージ",  value=f"`{_bar(disk.percent)}` {disk.percent:.1f}%  ({disk.used//1024**3:.1f}GB / {disk.total//1024**3:.1f}GB)", inline=False)
    embed.add_field(name="アップタイム", value=uptime, inline=True)
    embed.add_field(name="Ping",        value=f"{lat} ms", inline=True)
    embed.add_field(name="date.txt",    value=f"{dsz:.1f} KB", inline=True)
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
    gd = get_guild_data(guild.id)
    # 上書き前に古いコードを _codes から削除
    old_backup = gd.get("backup")
    if old_backup and old_backup.get("code"):
        data = load_data()
        data.get("_codes", {}).pop(old_backup["code"], None)
        save_data(data)
    gd["backup"] = backup
    set_guild_data(guild.id, gd)
    data = load_data()
    data.setdefault("_codes", {})[code] = str(guild.id)
    save_data(data)

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
    gd = get_guild_data(interaction.guild_id)
    if gd.get("backup"):
        ex = gd["backup"]
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
            print(f"[restore] ロール作成エラー {rd['name']}: {e}")

    await progress("カテゴリを復元中...")
    cat_map = {}
    for cd in sorted(backup.get("categories", []), key=lambda c: c["position"]):
        try:
            cat = await guild.create_category(name=cd["name"])
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
            if "text" in ct or "news" in ct:
                new_ch = await guild.create_text_channel(
                    name=chd["name"], category=cat,
                    nsfw=chd.get("nsfw", False), topic=chd.get("topic"),
                    slowmode_delay=chd.get("slowmode", 0))
                if chd.get("private"):
                    await new_ch.set_permissions(guild.default_role, view_channel=False)
                if log_ch is None and not chd.get("private"):
                    log_ch = new_ch
            elif "voice" in ct:
                nv = await guild.create_voice_channel(name=chd["name"], category=cat)
                if chd.get("private"):
                    await nv.set_permissions(guild.default_role, view_channel=False)
            elif "stage" in ct:
                await guild.create_stage_channel(name=chd["name"], category=cat)
            elif "forum" in ct:
                await guild.create_forum(name=chd["name"], category=cat)
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
@app_commands.describe(code="他サーバーのコード（省略=自サーバーのバックアップ）")
async def cmd_restore(interaction: discord.Interaction, code: str = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("管理者権限が必要です。", ephemeral=True)
        return
    backup = None
    if code:
        data = load_data()
        sgid = data.get("_codes", {}).get(code)
        if not sgid:
            await interaction.followup.send("コードが見つかりません。", ephemeral=True)
            return
        backup = data.get(sgid, {}).get("backup")
        if not backup:
            await interaction.followup.send("バックアップが存在しません。", ephemeral=True)
            return
    else:
        gd = get_guild_data(interaction.guild_id)
        backup = gd.get("backup")
        if not backup:
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
    "あっ…そこ…ふかく…いれないで…おなかがへん…になっちゃう…",
    "んぁっ…おまんこ…きもちよく…されたら…もうだめ…らめぇぇ…",
    "はぁ…はぁ…なかで…どくどく…してる…わかる…？…きもちぃ…",
    "や…やだっ…こんな…おくまで…とどいたら…いっちゃうぅ…",
    "くちゅくちゅ…っていってる…はずかしいから…きかないでぇ…",
    "んっ…もうぬれぬれ…なの…はやくいれてほしくて…たまんない…",
    "あっあっ…おねがい…もっとはやく…ぐりぐりして…おねがい…",
    "ふぁ…クリトリス…そんな…ちゅーしたら…こわれちゃうぅ…",
    "ん…ちくびがびんびん…になってるの…みないでぇ…きもちぃから…",
    "あぁ…おなか…ぽこぽこしてる…あなたのが…こんなに…おおきくて…",
    "だめ…やだ…でも…いかせてぇ…もう…がまんできない…いかせてぇぇ…",
    "んぁっ…ゆびで…なかを…かきまわしたら…だびょーって…でちゃうぅ…",
    "もうぐちょぐちょ…なの…きいてる？…ここ…ずっとうごかしてて…",
    "あっ…いっちゃう…ほんとに…いっちゃうから…とめないでぇぇ…",
    "はぁ…やばい…しりを…たたかれるの…なんで…こんなにきもちぃの…",
    "んっ…おっぱい…もみながら…したいの？…変態…でもきもちぃ…",
    "ふぁぁ…ぜんぶ…のみこんじゃった…おなかいっぱいぃ…よかった…",
    "やだっ…いきなり…うしろも…さわらないで…ぁでも…きもちよかった…",
    "あっ…ぜんぶ…きもちぃ…くりも…なかも…しりも…ぜんぶぅ…",
    "んんっ…せーえき…いっぱいでてる…あったかくて…きもちぃ…",
    "あぁ…おまんこが…ひくひくしてる…のわかる？…まだいけそう…",
    "ふぁ…あんな…おっきいの…いれたのに…もっとほしいなんて…わたしへん？…",
    "んもぅ…ぜんぶしらない…きもちよすぎて…あたまがとける…らめ…",
    "あっあっあっ…いく…いく…ほんとにいくぅぅ…とめないでぇぇ…",
    "はぁ…はぁ…なかで…びゅーって…されたら…また…いっちゃった…",
    "やっ…れろれろ…しながら…ゆびまで…いれないでぇ…きもちよすぎぃ…",
    "んっ…ふとももに…こすりつけてるの…わかってるから…ちゃんといれてぇ…",
    "ぁああ…しぼりとられてる…きもちぃ…もっとほしい…もっとぉ…",
    "やだ…くちで…してほしいの…おまんこ…なめてほしいの…おねがい…",
    "んあっ…いっしょに…いこ？…なかに…だしていいから…いっしょにぃ…",
    # ── 追加語録 ──────────────────────────────────────
    "ふぁっ…きもちぃよぉ…こんなの…しらなかった…まじで…やばいってぇ…",
    "あっ…おまんこ…ひろがってる…かんじする…もっとおしこんでぇ…",
    "んっ…クリいじりながら…おくまでついたら…らめぇぇぇ…こわれる…",
    "はぁっ…ぬれすぎて…じゅぽじゅぽおとしてる…はずかしいぃ…でもきもちぃ…",
    "あぁ…せなかからだかれながら…うごかれたら…なきそう…きもちぃ…",
    "んぁ…ちくびをこりこりしながら…したでなめられたら…いきかけた…",
    "やぁっ…うしろにいれながら…クリもさわったら…もうだめぇぇ…",
    "ふっ…ふっ…おなかのなかみちみちで…くるしい…でもきもちよすぎてぇ…",
    "んんっ…はげしくうごかないで…おねがい…すぐいっちゃうから…でもきもちぃ…",
    "あっ…あったかいなかに…いっぱいだしてくれたら…うれしいぃ…",
    "ふぁ…クリをくちでチュッチュしながら…ゆびをいれないでぇ…いきすぎるぅ…",
    "んっ…のみこめてるかな…ちゃんとぜんぶのみこみたい…おいしいぃ…",
    "あぁん…ぎゅってされながらうごかれると…こころもとけちゃいそう…",
    "はぁっ…おまんこがほしくておねだりしてるの…わかる？…いれてぇ…",
    "んあっ…ゆっくりゆっくりやったらズルいよぉ…もっとはやくしてぇ…",
    "ふぁっ…しおがでちゃう…でちゃう…とめられない…やばい…いっちゃうぅ…",
    "あっ…えっちなかおしてるっていわないでぇ…じぶんでわかってるからぁ…",
    "んっ…うごくたびにくちゅくちゅおとがして…はずかしすぎてりかんする…",
    "やっ…そんなにみつめながらしないでぇ…はずかしくていっちゃうぅ…",
    "ふぁ…とろとろになってきた…もうじぶんがわかんない…",
    "んぁあ…おしりのあなもさわらないでぇ…そこまだだいじょうぶじゃないから…",
    "あっあっ…はやすぎてついていけない…でもきもちぃからやめないで…",
    "はぁ…せんせい…もっとおしえて…えっちなきもちよさを…もっとぉ…",
    "んっ…なかがぎゅってしてるわかる？…はなしたくなくてぎゅってしてる…",
    "ふぁぁ…なんかいでもいかせてほしいぃ…",
    "あっ…おっぱいたぷたぷゆれてるのみてるんでしょ…変態…",
    "んんっ…ふかいとことんとんされたら…なきながらいっちゃう…",
    "ふぁ…おまんこのなかぜんぶみせてあげる…もっとみてぇ…",
    "あぁっ…くちでしごかれながらみあげたらめがあって…いきそう…",
    "んあっ…こんなおとでちゃってる…なかがびしょびしょだから…",
    "はあっ…3かいめなのにまだきもちぃ…おかしくなってきた…",
    "ふぁっ…うごくたびになかをかきまわされて…いまここいちばんきもちぃ…",
    "んっ…おしっこもれそうじゃなくて…しおがでちゃいそうなの…",
    "あっ…えっちなおとさせながらなかにいれてほしい…",
    "んぁ…うしろからだきしめながらここをくりくりしたら…ずるいよぉ…",
    "ふぁっ…んんっ…いきそういきそういきそう…とめないでぇぇ…",
    "あっ…おへそのしたがじーんってしてる…もうすぐいけそう…",
    "はぁ…いいこいいこってあたまなでながらしたら…だいすきになっちゃう…",
    "んっ…ふかすぎておなかまでとどいてるきがする…こわれちゃう…でもきもちぃ…",
    "あっあっ…んんっ…いくいくいくぅぅぅ…あぁぁぁぁぁっ…",
    "やぁっ…せなかにしながらちくびをつねったら…ほんとにやばい…",
    "んっ…きもちよくてなみだでてきた…なんでこんなにきもちぃの…",
    "ふぁぁ…なかにいっぱいだして…ぽたぽたたれてる…えっちだねわたし…",
    "あっ…またかたくなってる…まだするの？…うれしいぃ…",
    "はぁっ…もうじゅんびできてるから…はやくいれてぇ…おねだりしてる…",
    "んっ…きもちよすぎてことばがでない…ただただあっあっあっ…",
    "ふぁ…おまんこだけじゃなくてくちもおしりも…ぜんぶつかっていいよ…",
    "あぁ…もうぐちゃぐちゃなのにやめてくれない…もっとめちゃくちゃにして…",
    "んっ…きょうなんかいいかせてくれるの…もうかぞえるきりょくもない…",
    "ふぁ…かれしでもないのに…こんなにいかされたら…すきになっちゃうよ…",
    "あぁっ…みてて…わたしここがいちばんきもちぃから…ずっとここして…",
    "んんんっ…さいごにいっぱいなかにだして…おわりにして…おねがい…",
    # ── さらに追加語録 ──────────────────────────────────
    "あっ…ここ…きもちよすぎて…あしがたたない…もうたおれそう…",
    "んぁ…ずっとここで…うごかしてて…きもちよくてめがきえる…",
    "ふぁっ…さすったら…すぐぬれちゃった…じぶんでもびっくり…",
    "はぁ…おなかのそこが…きゅんきゅんしてる…はやくちょうだい…",
    "んっ…ぬれてるとこ…みないでぇ…でもみてほしいきもちもある…",
    "あぁ…ここに…すぽっていれたら…ぴったりはまる…きもちぃ…",
    "やっ…ずっといったりきたり…されたら…なんかでてきた…やばい…",
    "んんっ…にほんごわすれた…きもちよすぎて…あっあっしかいえない…",
    "ふぁっ…したから…くりくりされながら…ふかくつかれたら…らめらめ…",
    "あっ…そこ…とくべつにきもちよいとこ…よくわかったね…すごい…",
    "はぁっ…おしりをもちあげさせて…さらにおくまでいれないでぇ…こわれる…",
    "んぁっ…くちゅくちゅ…じゅぽじゅぽ…えっちなおとしかしてない…",
    "ふぁ…うごくたびに…ちくびがゆれて…じぶんでもきもちよくなってる…",
    "あぁっ…さわられるまえから…もうびちょびちょだった…ごめんなさい…",
    "んっ…かおにかかったぁ…はずかしい…でもきもちよかったよ…",
    "やぁ…なかでおっきくなるの…わかる…すごくきもちぃ…",
    "ふぁっ…じぶんのこえが…えっちすぎて…こわくなってきた…でもとまれない…",
    "あっ…きもちよくて…あしがぷるぷるしてる…もうたてない…",
    "んぁ…ゆびいれたまま…くりくりしたら…いちびょうでいった…",
    "はぁ…おまんこが…すっごいひくひくしてる…まだほしいってしてる…",
    "ふぁぁっ…はだかでだかれながら…キスしてほしい…すきなひとに…",
    "んんっ…いったのに…まだうごかしてるの…きもちよすぎておかしくなる…",
    "あっ…ちんちんのかたち…なかでかんじる…ここまでとどいてる…",
    "やぁ…だいすきなひとにいかされたら…ないてしまった…きもちぃ…",
    "ふぁ…ここをなめながら…ゆびをいれると…ちがうとこがいきそう…",
    "んぁっ…かれし…いやキミ…ちょっとまって…いきそうだから…ちょっとまって…",
    "はぁっ…うえにのりながら…うんどうするの…はずかしいけどきもちぃ…",
    "あっ…こんなにぬれてるのに…まだいじわる…いれてくれないの…",
    "んっ…ふとももをつかんで…はげしくされると…なかがきゅってする…",
    "ふぁぁ…3かいいかせてくれたら…なんでもします…だからもっとして…",
    "やぁっ…うしろからあたまをおさえて…されると…ほんとにやばい…",
    "あぁ…ひとさしゆびと…なかゆびで…いっぺんにいれないでぇ…ひろがっちゃう…",
    "んんっ…せなかをなでながら…されると…なんかかなしくなってなける…きもちぃから…",
    "ふぁっ…いちどにいっぱいきもちよくなると…あたまがまっしろになる…",
    "あっ…くちと…なかと…ゆびで…さんかしょいっぺんにされたら…もう…あぁ…",
    "んぁ…おしりのあな…ゆっくりほぐされてきた…こわいけどきもちぃ…",
    "はぁ…ずっとキスしながら…うごいてほしい…かおをみてほしい…",
    "ふぁっ…なかにはいってるの…わかる？…すごくきもちいいの…",
    "あぁっ…おかあさんになれるとこ…つっついたら…らめぇぇぇぇ…",
    "んっ…えっちなことしながら…すきってきかれたら…こたえられない…",
    "やぁ…もうじぶんがだれかわかんない…きもちよすぎて…とけてる…",
    "ふぁぁ…ちゅっちゅしながら…したで…ころころされたら…いった…",
    "あっ…きょうはなんかいいかせてくれるの？…もうかぞえてない…",
    "んんっ…このまま…あさになるまで…してほしい…",
    "はぁ…ぎゅってだきながら…なかにだしてくれたら…うれしくてなく…",
    "ふぁっ…もうここ…キミのかたちになってるかも…きもちぃ…",
    "あぁ…えっちなことしてる…わたし…でも…やめたくない…",
    "んっ…いっぱいいかせてくれて…ありがとう…だいすき…",
    "ふぁぁぁ…らめぇ…もうらめぇ…でも…やめないでぇぇぇ…",
    "あっあっあっあぁっ…いっちゃう…いっちゃう…いくぅぅぅぅっ！！",
]

async def check_lewd(message: discord.Message):
    text = message.content.lower()
    for kw in LEWD_KEYWORDS:
        if kw.lower() in text:
            reply_text = random.choice(LEWD_REPLIES)
            # h_flan.png をアイコンにしたWebhookで送信
            try:
                hooks = await message.channel.webhooks()
                wh = next((h for h in hooks if h.name == "ｴｯﾁﾅﾌﾗﾝﾁｬﾝ"), None)
                if wh is None:
                    # アイコン画像を読み込んでWebhookを作成
                    img_path = os.path.join(os.path.dirname(__file__), "h_flan.png")
                    if os.path.exists(img_path):
                        with open(img_path, "rb") as f:
                            avatar = f.read()
                        wh = await message.channel.create_webhook(
                            name="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ", avatar=avatar)
                    else:
                        wh = await message.channel.create_webhook(name="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ")
                await wh.send(reply_text, username="ｴｯﾁﾅﾌﾗﾝﾁｬﾝ")
            except Exception:
                # Webhookが使えない場合は通常返信にフォールバック
                await message.channel.send(reply_text)
            break

@bot.tree.command(name="lewd", description="えっち検出機能のON/OFFを切り替えます")
@app_commands.describe(scope="channel=このチャンネルのみ / server=サーバー全体", state="ON / OFF", channel="対象チャンネル（省略=実行チャンネル）")
async def cmd_lewd(interaction: discord.Interaction, scope: str = "channel", state: str = "ON", channel: discord.TextChannel = None):
    await safe_defer(interaction, ephemeral=True)
    if not interaction.user.guild_permissions.manage_channels:
        await interaction.followup.send("チャンネル管理権限が必要です。", ephemeral=True)
        return
    gd = get_guild_data(interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["lewd_server"] = on
        msg = f"サーバー全体のえっち検出を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("lewd_channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["lewd_channels"] = chs
        msg = f"{target.mention} のえっち検出を {'ON' if on else 'OFF'} にしました。"
    set_guild_data(interaction.guild_id, gd)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# 15. 画像取得 /h (NSFWチャンネル限定)
# ──────────────────────────────────────────────
RICK_GIFS = [
    "http://mamechosu.cloudfree.jp/dc/5655/cdn/gif/rick.gif",
    "http://mamechosu.cloudfree.jp/dc/5655/cdn/gif/rick1.gif",
]
# yande.re タグ設定（GL多め・BLなし・R18）
# グロ・残虐系タグ除外リスト
_GURO_TAGS = [
    "guro", "gore", "blood", "amputee", "ryona", "vore",
    "scat", "torture", "death", "decapitation", "wound",
    "bruise", "injury", "cannibal",
]

async def fetch_yande_pool(session: aiohttp.ClientSession) -> list:
    """
    yande.re から完全ランダムなNSFW画像をプールして返す。
    タグ指定なし (rating:e のみ) で完全ランダム、グロ系は除外。
    """
    urls = []
    seen = set()
    attempts = 0
    while len(urls) < 20 and attempts < 6:
        attempts += 1
        page = random.randint(1, 200)   # ランダムページ
        api_url = f"https://yande.re/post.json?tags=rating%3Aexplicit&limit=40&page={page}"
        try:
            async with session.get(
                api_url,
                headers={"User-Agent": "mamechosu-bot/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    continue
                posts = await resp.json()
                if not isinstance(posts, list):
                    continue
                for p in posts:
                    tag_str = p.get("tags", "").lower()
                    # グロ系タグがあればスキップ
                    if any(g in tag_str for g in _GURO_TAGS):
                        continue
                    fu  = p.get("file_url") or p.get("sample_url", "")
                    ext = fu.rsplit(".", 1)[-1].lower() if fu else ""
                    if fu and ext in ("jpg", "jpeg", "png", "gif", "webp") and fu not in seen:
                        urls.append(fu)
                        seen.add(fu)
        except Exception:
            continue
    return urls

@bot.tree.command(name="h", description="えっちな画像をランダムで取得します")
@app_commands.guild_only()
async def cmd_h(interaction: discord.Interaction):
    ch = interaction.channel
    if not (isinstance(ch, discord.TextChannel) and ch.nsfw):
        await interaction.response.send_message("このコマンドはNSFW（年齢制限）チャンネルでのみ使用できます。", ephemeral=True)
        return
    await safe_defer(interaction)
    if random.random() < 0.8:
        async with aiohttp.ClientSession() as session:
            pool = await fetch_yande_pool(session)
        if pool:
            url = random.choice(pool)
            # Discordが画像URLを埋め込み表示しない場合に備えてEmbedで包む
            ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
            if ext in ("jpg", "jpeg", "png", "gif", "webp"):
                embed = discord.Embed(color=0x2b2d31)
                embed.set_image(url=url)
                await interaction.followup.send(embed=embed)
            else:
                # 動画系はURLのまま（Embedに入れられない）
                await interaction.followup.send(url)
        else:
            await interaction.followup.send("画像の取得に失敗しました。")
    else:
        rick_url = random.choice(RICK_GIFS)
        rick_embed = discord.Embed(color=0x2b2d31)
        rick_embed.set_image(url=rick_url)
        await interaction.followup.send(embed=rick_embed)


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
        kasso = "超活発🔥"
        kasso_color = (87, 242, 135)
    elif msgs_per_day >= 50:
        kasso = "活発😊"
        kasso_color = (87, 200, 135)
    elif msgs_per_day >= 10:
        kasso = "普通😐"
        kasso_color = (254, 231, 92)
    elif msgs_per_day >= 1:
        kasso = "過疎気味😴"
        kasso_color = (237, 150, 69)
    else:
        kasso = "過疎💀"
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
            img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supiki.webp")
            avatar = None
            if os.path.exists(img_path):
                with open(img_path, "rb") as f:
                    avatar = f.read()
            wh = await channel.create_webhook(name="ｽﾋﾟｷ", avatar=avatar)
        return wh
    except Exception as e:
        print(f"[supiki] {e}")
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
    embed.add_field(name=f"付与済み ({len(ok)}件)", value="\n".join(f"✅ {n}" for n in ok) or "なし", inline=True)
    if ng:
        embed.add_field(name=f"不足 ({len(ng)}件)", value="\n".join(f"❌ {n}" for n in ng), inline=True)
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

def build_quote_image(text: str, author: str = "", theme_name: str = "dark") -> Image.Image:
    import random as _rnd
    th = QUOTE_THEMES.get(theme_name, QUOTE_THEMES["dark"])
    BG, FG, ACCENT, SUB = th["bg"], th["fg"], th["accent"], th["sub"]
    W, H, PAD = 700, 400, 50
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H * 0.15
        r = max(0, min(255, int(BG[0]+(255-BG[0])*t*0.3)))
        g = max(0, min(255, int(BG[1]+(255-BG[1])*t*0.3)))
        b = max(0, min(255, int(BG[2]+(255-BG[2])*t*0.3)))
        draw.line([(0,y),(W,y)], fill=(r,g,b))
    for _ in range(3000):
        x,y = _rnd.randint(0,W-1), _rnd.randint(0,H-1)
        v = _rnd.randint(-12,12)
        r2,g2,b2 = img.getpixel((x,y))
        draw.point((x,y), fill=(max(0,min(255,r2+v)),max(0,min(255,g2+v)),max(0,min(255,b2+v))))
    draw.rectangle([PAD-16,PAD,PAD-12,H-PAD], fill=ACCENT)
    qf = _load_font(110)
    draw.text((PAD+2,PAD-28), "“", font=qf, fill=ACCENT, anchor="lt")
    body_font = _load_font(30 if len(text)<=20 else (22 if len(text)<=50 else 16))
    max_w = W - PAD*2 - 20
    words = text.split() if any(c.isascii() and c.isalpha() for c in text) else list(text)
    lines_out, cur = [], ""
    for w in words:
        test = cur + w
        bbox = body_font.getbbox(test)
        if bbox[2]-bbox[0] > max_w and cur:
            lines_out.append(cur); cur = w
        else:
            cur = test
    if cur: lines_out.append(cur)
    lh = body_font.size + 10
    ty = (H - lh*len(lines_out)) // 2
    for line in lines_out:
        draw.text((PAD+10, ty), line, font=body_font, fill=FG, anchor="lt"); ty += lh
    if author:
        af = _load_font(18)
        draw.line([(W-PAD-200,H-PAD-28),(W-PAD,H-PAD-28)], fill=ACCENT, width=1)
        draw.text((W-PAD,H-PAD-6), f"— {author}", font=af, fill=SUB, anchor="rb")
    draw.rectangle([PAD,H-PAD+8,W-PAD,H-PAD+10], fill=ACCENT)
    return img

@bot.tree.command(name="quote", description="名言カード画像を生成します")
@app_commands.describe(text="名言の本文（200文字以内）", author="著者名（省略可）",
                       theme="dark/light/blue/green/red（デフォルト:dark）")
async def cmd_quote(interaction: discord.Interaction, text: str, author: str = "", theme: str = "dark"):
    await safe_defer(interaction)
    if len(text) > 200:
        await interaction.followup.send("200文字以内で入力してください。", ephemeral=True); return
    img = build_quote_image(text, author, theme if theme in QUOTE_THEMES else "dark")
    buf = BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    await interaction.followup.send(file=discord.File(buf, "quote.png"))


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
    return load_data().get("_global_channels", [])

def set_global_channels(channels: list):
    data = load_data(); data["_global_channels"] = channels; save_data(data)

async def get_or_create_webhook(channel: discord.TextChannel):
    try:
        for h in await channel.webhooks():
            if h.name == "GlobalChat": return h.url
        return (await channel.create_webhook(name="GlobalChat")).url
    except Exception: return None

async def relay_global_message(message: discord.Message):
    channels = get_global_channels()
    if not any(c["channel_id"] == message.channel.id for c in channels): return
    if not _check_rate(f"globalchat:{message.guild.id}", cooldown_sec=2.0): return
    if message.author.bot: return   # Bot発言はリレーしない
    content = (message.content or "") + "".join(f"\n{a.url}" for a in message.attachments)
    if not content.strip() or content.startswith("http"): return
    uname  = f"{message.author.display_name} @ {message.guild.name}"
    avatar = message.author.display_avatar.url
    async with aiohttp.ClientSession() as session:
        for c in channels:
            if c["channel_id"] == message.channel.id: continue
            try:
                async with session.post(c["webhook_url"],
                    json={"username": uname, "avatar_url": avatar, "content": content[:2000]},
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
async def _groq_check_atsumori(text: str) -> bool:
    """
    GROQを使ってメッセージが「熱盛」な内容かどうか判定する。
    熱盛 = スポーツや競技の熱く盛り上がった場面・好プレー・逆転・感動的な展開など。
    誤検知（たまたま熱盛っぽい普通の文）も一定割合で許容する仕様。
    """
    if not GROQ_API_KEY:
        # APIなしの場合: 「熱盛」「あつもり」が含まれていたら反応
        return any(w in text for w in ["熱盛", "あつもり", "アツモリ", "ATSUMORI"])
    try:
        prompt = (
            "あなたはテレビ朝日報道ステーションの熱盛コーナーの厳格な審査員です。"
            "次のメッセージが熱盛かどうか厳密に判定してください。\n\n"
            "【熱盛と判定する条件（全て満たすこと）】\n"
            "- スポーツ・競技・ゲームに関する話題であること\n"
            "- 劇的な好プレー・逆転・記録更新・感動的な場面の描写であること\n"
            "- 明らかな興奮・感動・驚きが伝わる内容であること\n"
            "- または「熱盛」「あつもり」という言葉そのものが含まれること\n\n"
            "【熱盛でない例】\n"
            "- 普通の日常会話\n"
            "- スポーツ以外の話題\n"
            "- 感情的でも熱くもない文章\n\n"
            f"メッセージ: 「{text}」\n\n"
            "熱盛なら「はい」、そうでなければ「いいえ」とだけ答えてください。"
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.3,
                },
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return False
                data   = await resp.json()
                result = data["choices"][0]["message"]["content"].strip()
                return "はい" in result
    except Exception:
        pass
    return False

async def check_atsumori(message: discord.Message):
    """熱盛を検知してatsumori.pngを送信する（0.5%の確率でランダム誤検知あり）"""
    text = message.content.strip()
    if not text or text.startswith("/") or text.startswith("http"):
        return

    is_real    = await _groq_check_atsumori(text)
    # 0.5%の確率で誤検知（本物ではない場合のみ）
    is_mistake = (not is_real) and (random.random() < 0.005)

    if not is_real and not is_mistake:
        return

    img_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atsumori.png")
    if not os.path.exists(img_path):
        return

    sent = await message.channel.send(
        file=discord.File(img_path, "atsumori.png"),
        reference=message,
    )
    # 誤検知の場合は画像にリプライして謝罪
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
    gd = get_guild_data(interaction.guild_id)
    on = state.upper() == "ON"
    if scope == "server":
        gd["atsumori_server"] = on
        msg = f"サーバー全体の熱盛検知を {'ON' if on else 'OFF'} にしました。"
    else:
        target = channel or interaction.channel
        chs = gd.get("atsumori_channels", [])
        if on and target.id not in chs:
            chs.append(target.id)
        elif not on and target.id in chs:
            chs.remove(target.id)
        gd["atsumori_channels"] = chs
        msg = f"{target.mention} の熱盛検知を {'ON' if on else 'OFF'} にしました。"
    set_guild_data(interaction.guild_id, gd)
    await interaction.followup.send(msg, ephemeral=True)

# ──────────────────────────────────────────────
# Bot 起動
# ──────────────────────────────────────────────
import os as _os
if not _os.environ.get("DEPLOY_MODE"):
    bot.run(TOKEN, log_handler=None)