"""
AstroBot — Discord ticket bot + Flask API
Railway env vars required: DISCORD_TOKEN, API_SECRET
"""
import discord
from discord.ext import commands
from flask import Flask, request, jsonify
import json, os, asyncio, threading, time
from datetime import datetime
from functools import wraps

# ──────────────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────────────
DATA_FILE     = os.path.join(os.path.dirname(__file__), "data.json")
PORT          = int(os.environ.get("PORT", 5000))
API_SECRET    = os.environ.get("API_SECRET", "AstroAstro!")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# ──────────────────────────────────────────────────────
#  DATA — thread-safe file I/O
# ──────────────────────────────────────────────────────
_lock = threading.Lock()

def load() -> dict:
    with _lock:
        if not os.path.exists(DATA_FILE):
            return {}
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

def save(data: dict):
    with _lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def gd(data: dict, guild_id) -> dict:
    k = str(guild_id)
    if k not in data:
        data[k] = {"panels": {}, "tickets": {}}
    data[k].setdefault("panels", {})
    data[k].setdefault("tickets", {})
    return data[k]

# ──────────────────────────────────────────────────────
#  BOT
# ──────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds           = True
intents.members          = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)

# ── Close helper ──────────────────────────────────────
async def do_close(channel: discord.TextChannel, actor):
    data = load()
    g    = gd(data, channel.guild.id)
    key  = str(channel.id)
    embed = discord.Embed(
        description=f"🔒 Closed by **{actor.display_name}** — deleting in 5 seconds…",
        color=0x111111,
    )
    try:
        await channel.send(embed=embed)
    except Exception:
        pass
    await asyncio.sleep(5)
    if key in g["tickets"]:
        del g["tickets"][key]
        save(data)
    try:
        await channel.delete(reason=f"Ticket closed by {actor}")
    except Exception:
        pass

# ── ?close command ────────────────────────────────────
@bot.command(name="close")
async def cmd_close(ctx: commands.Context):
    data = load()
    g    = gd(data, ctx.guild.id)
    key  = str(ctx.channel.id)
    if key not in g["tickets"]:
        await ctx.send("❌ This is not a ticket channel.", delete_after=5)
        return
    info   = g["tickets"][key]
    panel  = g["panels"].get(str(info.get("panel_id")), {})
    staff  = [int(r) for r in panel.get("staff_roles", [])]
    is_adm = ctx.author.guild_permissions.administrator
    is_own = ctx.author.id == info.get("user_id")
    is_stf = any(r.id in staff for r in ctx.author.roles)
    if not (is_adm or is_own or is_stf):
        await ctx.send("❌ You don't have permission to close this ticket.", delete_after=5)
        return
    await do_close(ctx.channel, ctx.author)

# ── Close button view ─────────────────────────────────
class CloseView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="astro_close_ticket",
    )
    async def close_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        data = load()
        g    = gd(data, interaction.guild_id)
        key  = str(interaction.channel_id)
        if key not in g["tickets"]:
            await interaction.response.send_message("This is not a tracked ticket.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await do_close(interaction.channel, interaction.user)

# ── Panel button view ─────────────────────────────────
class PanelView(discord.ui.View):
    def __init__(self, panel_id: str, guild_id: int):
        super().__init__(timeout=None)
        data  = load()
        g     = gd(data, guild_id)
        panel = g["panels"].get(str(panel_id))
        if not panel:
            return
        for i, b in enumerate(panel.get("buttons", [])):
            emoji = b.get("emoji") or None
            if emoji and not emoji.strip():
                emoji = None
            self.add_item(OpenBtn(
                panel_id=str(panel_id),
                guild_id=guild_id,
                btn_index=i,
                label=b["label"],
                emoji=emoji,
            ))

class OpenBtn(discord.ui.Button):
    def __init__(self, *, panel_id, guild_id, btn_index, label, emoji):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label,
            emoji=emoji,
            custom_id=f"astro_open:{guild_id}:{panel_id}:{btn_index}",
        )
        self.panel_id  = panel_id
        self.guild_id  = guild_id
        self.btn_index = btn_index

    async def callback(self, interaction: discord.Interaction):
        await open_ticket(interaction, self.panel_id, self.btn_index)

async def open_ticket(interaction: discord.Interaction, panel_id: str, btn_index: int):
    data  = load()
    g     = gd(data, interaction.guild_id)
    panel = g["panels"].get(str(panel_id))

    if not panel:
        await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
        return

    user  = interaction.user
    guild = interaction.guild

    # Duplicate check
    user_open = [
        c for c, t in g["tickets"].items()
        if t.get("user_id") == user.id and str(t.get("panel_id")) == str(panel_id)
    ]
    max_t = panel.get("max_tickets", 1)
    if len(user_open) >= max_t:
        await interaction.response.send_message(
            f"❌ You already have {len(user_open)} open ticket(s) for this panel (max {max_t}).",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Permission overwrites
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            manage_channels=True, manage_messages=True
        ),
    }
    for rid in panel.get("staff_roles", []):
        r = guild.get_role(int(rid))
        if r:
            ow[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_messages=True
            )
    for rid in panel.get("ping_roles", []):
        r = guild.get_role(int(rid))
        if r and r not in ow:
            ow[r] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    num   = len(g["tickets"]) + 1
    sname = user.display_name.replace(" ", "-").lower()[:20]
    cname = f"ticket-{sname}-{num}"

    # Create or find Tickets category
    cat = discord.utils.get(guild.categories, name="Tickets")
    if not cat:
        cat = await guild.create_category("Tickets")

    ch = await guild.create_text_channel(name=cname, overwrites=ow, category=cat, reason="Ticket opened")

    g["tickets"][str(ch.id)] = {
        "user_id":      user.id,
        "user_name":    str(user),
        "panel_id":     panel_id,
        "btn_index":    btn_index,
        "channel_name": cname,
        "opened_at":    datetime.utcnow().isoformat(),
    }
    save(data)

    btn_cfg = panel.get("buttons", [])[btn_index] if btn_index < len(panel.get("buttons", [])) else {}
    pings   = " ".join([f"<@{user.id}>"] + [f"<@&{r}>" for r in panel.get("ping_roles", [])])
    embed   = discord.Embed(
        title=btn_cfg.get("label", "Ticket"),
        description=btn_cfg.get("ticket_embed_text", "Please describe your issue."),
        color=0x111111,
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text=f"Opened by {user.display_name} • #{num} • ?close to close")
    await ch.send(content=pings, embed=embed, view=CloseView(ch.id))
    await interaction.followup.send(f"✅ Ticket opened: {ch.mention}", ephemeral=True)

# ── on_ready ──────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"[BOT] Logged in as {bot.user} (ID: {bot.user.id})")
    data = load()
    pc = 0
    for gid, g in data.items():
        for pid in g.get("panels", {}):
            try:
                bot.add_view(PanelView(panel_id=pid, guild_id=int(gid)))
                pc += 1
            except Exception as e:
                print(f"[BOT] Could not restore view pid={pid}: {e}")
        for cid in g.get("tickets", {}):
            try:
                bot.add_view(CloseView(channel_id=int(cid)))
            except Exception:
                pass
    print(f"[BOT] Restored {pc} panel view(s)")

# ──────────────────────────────────────────────────────
#  FLASK API
# ──────────────────────────────────────────────────────
api = Flask(__name__)

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        secret = request.headers.get("X-API-Secret") or request.args.get("secret", "")
        if secret != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

@api.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Secret"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp

@api.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = api.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"]  = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Secret"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

# ── Health ────────────────────────────────────────────
@api.route("/", methods=["GET"])
def root():
    return jsonify({"status": "AstroBot API online", "version": "3.0"})

@api.route("/api/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "ts": time.time()})

# ── Stats ─────────────────────────────────────────────
@api.route("/api/stats", methods=["GET"])
@require_auth
def stats():
    data    = load()
    panels  = sum(len(g.get("panels",  {})) for g in data.values())
    tickets = sum(len(g.get("tickets", {})) for g in data.values())
    return jsonify({
        "guilds":       len(bot.guilds),
        "total_panels": panels,
        "open_tickets": tickets,
        "bot_name":     str(bot.user)    if bot.user else "Loading…",
        "bot_id":       str(bot.user.id) if bot.user else None,
    })

# ── Guilds ────────────────────────────────────────────
@api.route("/api/guilds", methods=["GET"])
@require_auth
def guilds():
    return jsonify([
        {
            "id":           str(g.id),
            "name":         g.name,
            "icon":         str(g.icon.url) if g.icon else None,
            "member_count": g.member_count,
        }
        for g in bot.guilds
    ])

# ── Guild detail ──────────────────────────────────────
@api.route("/api/guild/<gid>/info", methods=["GET"])
@require_auth
def guild_info(gid):
    guild = bot.get_guild(int(gid))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    data = load()
    g    = gd(data, gid)
    return jsonify({
        "id":           str(guild.id),
        "name":         guild.name,
        "icon":         str(guild.icon.url) if guild.icon else None,
        "member_count": guild.member_count,
        "roles":        [{"id": str(r.id), "name": r.name} for r in reversed(guild.roles) if not r.is_default()],
        "channels":     [{"id": str(c.id), "name": c.name} for c in guild.text_channels],
        "panels":       g["panels"],
        "tickets":      g["tickets"],
    })

# ── Create panel ──────────────────────────────────────
@api.route("/api/guild/<gid>/panels", methods=["POST"])
@require_auth
def create_panel(gid):
    body  = request.get_json(force=True, silent=True) or {}
    guild = bot.get_guild(int(gid))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404

    channel_id = body.get("channel_id")
    if not channel_id:
        return jsonify({"error": "channel_id required"}), 400
    ch = guild.get_channel(int(channel_id))
    if not ch:
        return jsonify({"error": "Channel not found"}), 404

    buttons = body.get("buttons", [])
    if not buttons:
        return jsonify({"error": "At least one button required"}), 400

    data = load()
    g    = gd(data, gid)

    # Unique panel id
    pid = str(len(g["panels"]) + 1)
    while pid in g["panels"]:
        pid = str(int(pid) + 1)

    panel = {
        "channel_id":  int(channel_id),
        "title":       body.get("title", "Support"),
        "description": body.get("description", ""),
        "max_tickets": int(body.get("max_tickets", 1)),
        "ping_roles":  [int(r) for r in body.get("ping_roles",  [])],
        "staff_roles": [int(r) for r in body.get("staff_roles", [])],
        "buttons":     buttons,
        "message_id":  None,
        "created_at":  datetime.utcnow().isoformat(),
    }
    g["panels"][pid] = panel
    save(data)

    # Use a Future so Flask waits for the Discord message to actually send
    # This fixes the race condition where the panel saved but embed never posted
    future = asyncio.run_coroutine_threadsafe(_post_panel(gid, pid, panel, ch), bot.loop)
    try:
        future.result(timeout=15)  # wait up to 15s for Discord to confirm
    except Exception as e:
        print(f"[API] Panel post error: {e}")
        # Panel is saved — Discord message just failed, not fatal
        return jsonify({"success": True, "panel_id": pid, "warning": f"Saved but Discord post failed: {e}"})

    return jsonify({"success": True, "panel_id": pid})

async def _post_panel(gid, pid, panel, ch):
    """Post the panel embed+buttons to Discord and save the message_id."""
    embed = discord.Embed(
        title=panel["title"],
        description=panel["description"] or None,
        color=0x111111,
        timestamp=datetime.utcnow(),
    )
    embed.set_footer(text="Click a button below to open a ticket.")
    view = PanelView(panel_id=pid, guild_id=int(gid))
    bot.add_view(view)
    msg = await ch.send(embed=embed, view=view)
    # Save message_id back to data
    d2 = load()
    if str(gid) in d2 and pid in d2[str(gid)]["panels"]:
        d2[str(gid)]["panels"][pid]["message_id"] = msg.id
        save(d2)
    print(f"[BOT] Panel {pid} posted → message {msg.id} in #{ch.name}")

# ── Delete panel ──────────────────────────────────────
@api.route("/api/guild/<gid>/panels/<pid>", methods=["DELETE"])
@require_auth
def delete_panel(gid, pid):
    data = load()
    g    = gd(data, gid)
    if pid not in g["panels"]:
        return jsonify({"error": "Panel not found"}), 404
    panel = g["panels"].pop(pid)
    save(data)

    async def remove_message():
        guild = bot.get_guild(int(gid))
        if not guild:
            return
        ch = guild.get_channel(panel["channel_id"])
        if ch and panel.get("message_id"):
            try:
                m = await ch.fetch_message(panel["message_id"])
                await m.delete()
            except Exception:
                pass

    asyncio.run_coroutine_threadsafe(remove_message(), bot.loop)
    return jsonify({"success": True})

# ── Tickets ───────────────────────────────────────────
@api.route("/api/guild/<gid>/tickets", methods=["GET"])
@require_auth
def get_tickets(gid):
    data  = load()
    g     = gd(data, gid)
    guild = bot.get_guild(int(gid))
    out   = []
    for cid, info in g["tickets"].items():
        ch = guild.get_channel(int(cid)) if guild else None
        out.append({
            **info,
            "channel_id":   cid,
            "channel_name": ch.name if ch else info.get("channel_name", "deleted"),
        })
    return jsonify(out)

@api.route("/api/guild/<gid>/tickets/<cid>/close", methods=["POST"])
@require_auth
def close_ticket(gid, cid):
    guild = bot.get_guild(int(gid))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    ch = guild.get_channel(int(cid))
    if not ch:
        data = load()
        g    = gd(data, gid)
        g["tickets"].pop(cid, None)
        save(data)
        return jsonify({"success": True})
    asyncio.run_coroutine_threadsafe(do_close(ch, bot.user), bot.loop)
    return jsonify({"success": True})

# ── Send message ──────────────────────────────────────
@api.route("/api/guild/<gid>/send", methods=["POST"])
@require_auth
def send_message(gid):
    body  = request.get_json(force=True, silent=True) or {}
    guild = bot.get_guild(int(gid))
    if not guild:
        return jsonify({"error": "Guild not found"}), 404
    ch = guild.get_channel(int(body.get("channel_id", 0)))
    if not ch:
        return jsonify({"error": "Channel not found"}), 404
    asyncio.run_coroutine_threadsafe(ch.send(body.get("message", "")), bot.loop)
    return jsonify({"success": True})

# ──────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────
def run_flask():
    api.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"[API] Flask running on port {PORT}")
    bot.run(DISCORD_TOKEN, log_handler=None)