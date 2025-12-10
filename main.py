from typing import Optional, Tuple, List
import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio, aiofiles, json, os, random, re
from datetime import datetime, timedelta
from typing import Optional, List
from dotenv import load_dotenv


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN") 
if not TOKEN:
    raise ValueError("Hey you put the discord tocken :/")

CONFIG_PATH = "config.json"

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def has_perm(member: discord.Member, cfg: dict, perm: str) -> bool:
    if member.id == member.guild.owner_id:
        return True
    if member.guild_permissions.manage_guild:
        return True
    allowed = cfg.get("permissions", {}).get(perm, [])
    try:
        member_role_ids = {r.id for r in member.roles}
    except Exception:
        member_role_ids = set()
    return any(int(rid) in member_role_ids for rid in allowed)

# ====================== CONFIG ======================
DEFAULT_CONFIG = {
    "permissions": {
        "Staff-Test": [], "Staff-Promote": [], "Staff-Demote": [], "Staff-Remove": [],
        "Staff-Questions": [], "Staff-History": [], "Staff-LOA": [], "Config-GUI": [], "Level-GUI": []
    },
    "settings": {
        "test_question_number": 10,
        "test_time_minutes": 10,
        "default_role": None,
        "min_answer": 5
    },
    "questions": [],
    "history": {},
    "loa_tasks": {},
    "prev_nicks": {},
    "levels": {}
}

async def load_config():
    if not os.path.exists(CONFIG_PATH):
        async with aiofiles.open(CONFIG_PATH, "w", encoding="utf-8") as f:
            await f.write(json.dumps(DEFAULT_CONFIG, indent=4, ensure_ascii=False))
        return DEFAULT_CONFIG.copy()
    async with aiofiles.open(CONFIG_PATH, "r", encoding="utf-8") as f:
        content = await f.read()
    try:
        cfg = json.loads(content)
    except Exception:
        cfg = DEFAULT_CONFIG.copy()
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v
    return cfg

async def save_config(cfg):
    async with aiofiles.open(CONFIG_PATH, "w", encoding="utf-8") as f:
        await f.write(json.dumps(cfg, indent=4, ensure_ascii=False))

async def add_history(cfg, user_id: int, action: str, by: Optional[int] = None):
    key = str(user_id)
    entry = {"ts": datetime.utcnow().isoformat(), "action": action, "by": by}
    cfg.setdefault("history", {}).setdefault(key, []).append(entry)
    cfg["history"][key] = cfg["history"][key][-1000:]
    await save_config(cfg)

@bot.event
async def on_ready():
    bot.cfg = await load_config()
    print(f"[READY] {bot.user} online! ID: {bot.user.id}")
    try:
        synced = await bot.tree.sync()
        print(f"[SYNCED] {len(synced)} commands synced")
    except Exception as e:
        print("Sync failed:", e)
    check_loa.start()  #LOA

# ----------------- Permission GUI & Role-Weight Management -----------------
class RoleWeightModal(discord.ui.Modal, title="Add Role to Levels (Weight)"):
    role_select: discord.ui.TextInput  # dummy for typing, actual select done in view
    weight = discord.ui.TextInput(label="Weight (integer)", max_length=6, placeholder="e.g. 1", required=True)

    def __init__(self, cfg, guild: discord.Guild, role_id: int):
        super().__init__()
        self.cfg = cfg
        self.guild = guild
        self.role_id = role_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            w = int(self.weight.value)
        except Exception:
            return await interaction.response.send_message("Weight must be an integer.", ephemeral=True)
        self.cfg.setdefault("levels", {})[str(self.role_id)] = int(w)
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.send_message(f"Role <@&{self.role_id}> added with weight {w}", ephemeral=True)

class RoleWeightView(discord.ui.View):
    def __init__(self, cfg, user_id, guild: discord.Guild):
        super().__init__(timeout=300)
        self.cfg = cfg
        self.user_id = user_id
        self.guild = guild

        options = [discord.SelectOption(label=r.name, value=str(r.id)) for r in guild.roles if not r.is_default()][:25]
        self.select = discord.ui.Select(placeholder="Select role...", options=options, min_values=1, max_values=1)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Add Role (set weight)", style=discord.ButtonStyle.success, emoji="➕")
    async def add_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.select.values:
            return await interaction.response.send_message("No role selected.", ephemeral=True)
        rid = int(self.select.values[0])
        modal = RoleWeightModal(self.cfg, self.guild, rid)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="⛔")
    async def remove_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.select.values:
            return await interaction.response.send_message("No role selected.", ephemeral=True)
        rid = str(self.select.values[0])
        if rid in self.cfg.get("levels", {}):
            self.cfg["levels"].pop(rid, None)
            await save_config(self.cfg); bot.cfg = self.cfg
            await interaction.response.send_message(f"Removed role <@&{rid}> from levels.", ephemeral=True)
        else:
            await interaction.response.send_message("That role is not in the levels list.", ephemeral=True)

    @discord.ui.button(label="Remove-all", style=discord.ButtonStyle.secondary, emoji="⚠️")
    async def remove_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["levels"] = {}
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.send_message("All role weight entries removed from configuration.", ephemeral=True)

class PermGUIView(discord.ui.View):
    def __init__(self, cfg, user_id):
        super().__init__(timeout=300)
        self.cfg = cfg
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def open_selector(self, interaction: discord.Interaction, key: str, label: str, emoji: str):
        view = RoleSelectView(self.cfg, interaction.guild, key, interaction.user.id)
        current = [interaction.guild.get_role(int(r)).mention for r in self.cfg["permissions"].get(key, []) if interaction.guild.get_role(int(r))]
        await interaction.response.send_message(f"{emoji} **{label}**\nCurrent: {', '.join(current) or 'None'}", view=view, ephemeral=True)

    @discord.ui.button(label="Staff Test", style=discord.ButtonStyle.primary, emoji="📝")
    async def test(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-Test", "Staff Test", "📝")

    @discord.ui.button(label="Promote", style=discord.ButtonStyle.blurple, emoji="⬆️")
    async def promote(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-Promote", "Promote", "⬆️")

    @discord.ui.button(label="Demote", style=discord.ButtonStyle.secondary, emoji="⬇️")
    async def demote(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-Demote", "Demote", "⬇️")

    @discord.ui.button(label="Remove Staff", style=discord.ButtonStyle.danger, emoji="🛑")
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-Remove", "Remove Staff", "🛑")

    @discord.ui.button(label="Questions", style=discord.ButtonStyle.secondary, emoji="❓")
    async def questions(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-Questions", "Manage Questions", "❓")

    @discord.ui.button(label="History", style=discord.ButtonStyle.secondary, emoji="📜")
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-History", "View History", "📜")

    @discord.ui.button(label="LOA", style=discord.ButtonStyle.secondary, emoji="🏖️")
    async def loa(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Staff-LOA", "LOA", "🏖️")

    @discord.ui.button(label="Config", style=discord.ButtonStyle.success, emoji="⚙️")
    async def config(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.open_selector(interaction, "Config-GUI", "Config Access", "⚙️")

    @discord.ui.button(label="Levels", style=discord.ButtonStyle.primary, emoji="🎚️")
    async def levels(self, interaction: discord.Interaction, _: discord.ui.Button):
        await open_level_gui(interaction)

    @discord.ui.button(label="🧪Roles", style=discord.ButtonStyle.primary, emoji="🧪")
    async def roles(self, interaction: discord.Interaction, _: discord.ui.Button):
        # open RoleWeightView for adding/removing weight-configured roles
        view = RoleWeightView(self.cfg, interaction.user.id, interaction.guild)
        # build current list
        lst = []
        for rid, weight in sorted(self.cfg.get("levels", {}).items(), key=lambda x: x[1]):
            role = interaction.guild.get_role(int(rid))
            if role:
                lst.append(f"{role.mention} — {weight}")
            else:
                lst.append(f"<deleted role {rid}> — {weight}")
        current = "\n".join(lst) or "None"
        await interaction.response.send_message(f"🧪 **Roles (weight list)**\nCurrent:\n{current}", view=view, ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self, cfg, guild, perm_key, user_id):
        super().__init__(timeout=180)
        self.cfg = cfg
        self.guild = guild
        self.perm_key = perm_key
        self.user_id = user_id

        options = [discord.SelectOption(label=r.name, value=str(r.id)) for r in guild.roles if not r.is_default()][:25]
        self.select = discord.ui.Select(placeholder="Select roles...", options=options, min_values=1, max_values=10)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success, emoji="➕")
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button):
        added = [r for r in self.select.values if r not in self.cfg["permissions"][self.perm_key]]
        self.cfg["permissions"][self.perm_key].extend(added)
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.send_message(f"Added {len(added)} role(s)", ephemeral=True)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="➖")
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["permissions"][self.perm_key] = [r for r in self.cfg["permissions"][self.perm_key] if r not in self.select.values]
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.send_message("Removed selected roles", ephemeral=True)

# ----------------- Config GUI (settings and default role) -----------------
class NumberEditor(discord.ui.View):
    def __init__(self, cfg, key, title, user_id):
        super().__init__(timeout=180)
        self.cfg = cfg
        self.key = key
        self.title = title
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    def embed(self):
        val = self.cfg["settings"].get(self.key, 0)
        return discord.Embed(title=self.title, description=f"Current: `{val}`", color=0x00ff00)

    @discord.ui.button(label="+1", style=discord.ButtonStyle.success, emoji="⬆️")
    async def inc(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["settings"][self.key] = self.cfg["settings"].get(self.key, 0) + 1
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="-1", style=discord.ButtonStyle.danger, emoji="⬇️")
    async def dec(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["settings"][self.key] = max(1, self.cfg["settings"].get(self.key, 0) - 1)
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.primary, emoji="💾")
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Saved!", view=None)

class DefaultRoleEditor(discord.ui.View):
    def __init__(self, cfg, user_id, guild):
        super().__init__(timeout=180)
        self.cfg = cfg
        self.user_id = user_id
        self.guild = guild
        options = [discord.SelectOption(label=r.name, value=str(r.id)) for r in guild.roles if not r.is_default()][:25]
        self.select = discord.ui.Select(placeholder="Select default role", options=options, min_values=1, max_values=1)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Save Default Role", style=discord.ButtonStyle.success, emoji="💾")
    async def save_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["settings"]["default_role"] = int(self.select.values[0])
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.edit_message(content=f"Default role set to <@&{self.select.values[0]}>", view=None)

class ConfigGUI(discord.ui.View):
    def __init__(self, cfg, user_id, guild):
        super().__init__(timeout=300)
        self.cfg = cfg
        self.user_id = user_id
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Question Count", style=discord.ButtonStyle.primary, emoji="🔢")
    async def qcount(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = NumberEditor(self.cfg, "test_question_number", "Question Count", interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Test Time (min)", style=discord.ButtonStyle.primary, emoji="⏱️")
    async def time(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = NumberEditor(self.cfg, "test_time_minutes", "Test Duration (minutes)", interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Min Answers", style=discord.ButtonStyle.primary, emoji="✅")
    async def minans(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = NumberEditor(self.cfg, "min_answer", "Minimum Correct Answers", interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Default Role", style=discord.ButtonStyle.secondary, emoji="🎯")
    async def default_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = DefaultRoleEditor(self.cfg, interaction.user.id, interaction.guild)
        await interaction.response.send_message("Select Default Role", view=view, ephemeral=True)

    @discord.ui.button(label="Roles (weight list)", style=discord.ButtonStyle.primary, emoji="🧪")
    async def roles_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = RoleWeightView(self.cfg, interaction.user.id, interaction.guild)
        lst = []
        for rid, weight in sorted(self.cfg.get("levels", {}).items(), key=lambda x: x[1]):
            role = interaction.guild.get_role(int(rid))
            if role:
                lst.append(f"{role.mention} — {weight}")
            else:
                lst.append(f"<deleted role {rid}> — {weight}")
        current = "\n".join(lst) or "None"
        await interaction.response.send_message(f"🧪 **Roles (weight list)**\nCurrent:\n{current}", view=view, ephemeral=True)

@bot.tree.command(name="configgui", description="Bot Configuration Panel")
async def configgui(interaction: discord.Interaction):
    if not has_perm(interaction.user, bot.cfg, "Config-GUI"):
        return await interaction.response.send_message("Access denied", ephemeral=True)
    view = ConfigGUI(bot.cfg, interaction.user.id, interaction.guild)
    await interaction.response.send_message("Bot Settings", view=view, ephemeral=True)

# ----------------- Questions management (add / paginated view / remove) -----------------
class AddQuestionModal(discord.ui.Modal, title="Add New Question"):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.question = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph, max_length=1000)
        self.answer = discord.ui.TextInput(label="Correct Answer", style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.question)
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        self.cfg.setdefault("questions", []).append({"q": self.question.value, "a": self.answer.value})
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.send_message("Question added!", ephemeral=True)

class QuestionsView(discord.ui.View):
    def __init__(self, cfg, user_id):
        super().__init__(timeout=300)
        self.cfg = cfg
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Add Question", style=discord.ButtonStyle.success, emoji="➕")
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddQuestionModal(self.cfg))

    @discord.ui.button(label="View All", style=discord.ButtonStyle.primary, emoji="📄")
    async def view_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = QuestionsPaginatedView(self.cfg, interaction.user.id)
        await view.send_page(interaction, 0)

class QuestionsPaginatedView(discord.ui.View):
    def __init__(self, cfg, user_id, per_page: int = 10):
        super().__init__(timeout=600)
        self.cfg = cfg
        self.user_id = user_id
        self.per_page = per_page
        self.current_page = 0

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    def build_embed(self, page: int) -> discord.Embed:
        questions = self.cfg.get("questions", [])
        total = len(questions)
        pages = max(1, (total + self.per_page - 1) // self.per_page)
        page = max(0, min(page, pages - 1))
        start = page * self.per_page
        end = start + self.per_page
        embed = discord.Embed(title=f"Questions (page {page+1}/{pages})")
        if total == 0:
            embed.description = "No questions."
            return embed
        desc_lines = []
        for idx, q in enumerate(questions[start:end], start + 1):
            qtext = q.get("q", "").replace("`", "'")
            atext = q.get("a", "").replace("`", "'")
            desc_lines.append(f"**{idx}.** Q: `{qtext}`\nA: `{atext}`")
        embed.description = "\n\n".join(desc_lines)
        return embed

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await interaction.response.edit_message(embed=self.build_embed(self.current_page), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        total = len(self.cfg.get("questions", []))
        pages = max(1, (total + self.per_page - 1) // self.per_page)
        self.current_page = min(pages - 1, self.current_page + 1)
        await interaction.response.edit_message(embed=self.build_embed(self.current_page), view=self)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, emoji="⛔")
    async def remove(self, interaction: discord.Interaction, _: discord.ui.Button):
        modal = RemoveQuestionModal(self.cfg, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Remove-all", style=discord.ButtonStyle.secondary, emoji="⚠️")
    async def remove_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cfg["questions"] = []
        await save_config(self.cfg); bot.cfg = self.cfg
        await interaction.response.edit_message(content="All questions removed.", embed=self.build_embed(0), view=self)

    async def send_page(self, interaction: discord.Interaction, page: int):
        self.current_page = page
        embed = self.build_embed(page)
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

class RemoveQuestionModal(discord.ui.Modal, title="Remove Question by Number"):
    number = discord.ui.TextInput(label="Question number to remove", placeholder="e.g. 3", required=True, max_length=6)

    def __init__(self, cfg, pagview: QuestionsPaginatedView):
        super().__init__()
        self.cfg = cfg
        self.pagview = pagview

    async def on_submit(self, interaction: discord.Interaction):
        try:
            idx = int(self.number.value)
        except Exception:
            return await interaction.response.send_message("Invalid number.", ephemeral=True)
        if idx < 1 or idx > len(self.cfg.get("questions", [])):
            return await interaction.response.send_message("Question number out of range.", ephemeral=True)
        removed = self.cfg["questions"].pop(idx - 1)
        await save_config(self.cfg); bot.cfg = self.cfg
        # refresh page
        await interaction.response.send_message(f"Removed question {idx}.", ephemeral=True)

@bot.tree.command(name="questions", description="Manage Test Questions")
async def questions(interaction: discord.Interaction):
    if not has_perm(interaction.user, bot.cfg, "Staff-Questions"):
        return await interaction.response.send_message("No permission", ephemeral=True)
    view = QuestionsView(bot.cfg, interaction.user.id)
    await interaction.response.send_message("Question Manager", view=view, ephemeral=True)

# ----------------- MarkAnswer, TestControl and stafftest -----------------
class MarkAnswer(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.result = None

    async def interaction_check(self, interaction: discord.Interaction):
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Correct", style=discord.ButtonStyle.success, emoji="✅")
    async def correct(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.result = True
        await interaction.response.edit_message(content="Marked Correct", view=None)
        self.stop()

    @discord.ui.button(label="Wrong", style=discord.ButtonStyle.danger, emoji="❌")
    async def wrong(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.result = False
        await interaction.response.edit_message(content="Marked Wrong", view=None)
        self.stop()

class TestControl(discord.ui.View):
    def __init__(self, cfg, tester, candidate, vc):
        super().__init__(timeout=900)
        self.cfg = cfg
        self.tester = tester
        self.candidate = candidate
        self.vc = vc
        self.score = 0

    @discord.ui.button(label="Start Test", style=discord.ButtonStyle.success, emoji="▶️")
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        if len(self.cfg.get("questions", [])) < self.cfg["settings"]["test_question_number"]:
            await interaction.followup.send("Not enough questions in database", ephemeral=True)
            try:
                await self.vc.delete()
            except Exception:
                pass
            return

        try:
            await self.vc.set_permissions(interaction.guild.default_role, connect=False)
            await self.vc.set_permissions(self.tester, connect=True)
            await self.vc.set_permissions(self.candidate, connect=True)
        except Exception:
            pass

        questions = random.sample(self.cfg.get("questions", []), k=self.cfg["settings"]["test_question_number"])
        for idx, q in enumerate(questions, 1):
            view = MarkAnswer(self.tester.id)
            await interaction.followup.send(
                embed=discord.Embed(title=f"Question {idx}")
                    .add_field(name="Question", value=q["q"], inline=False)
                    .add_field(name="Answer", value=q["a"], inline=False),
                view=view, ephemeral=True
            )
            await view.wait()
            if view.result is True:
                self.score += 1

        passed = self.score >= self.cfg["settings"]["min_answer"]
        if passed and self.cfg["settings"].get("default_role"):
            role = interaction.guild.get_role(int(self.cfg["settings"]["default_role"]))
            if role:
                try:
                    await self.candidate.add_roles(role, reason="Passed test")
                    await update_level_roles(interaction.guild, self.candidate, self.cfg)
                except Exception:
                    pass

        await interaction.followup.send(f"Result: {'Passed' if passed else 'Failed'} | Score: {self.score}/{len(questions)}", ephemeral=True)
        await add_history(self.cfg, self.candidate.id, f"Test {'passed' if passed else 'failed'} ({self.score}/{len(questions)})", self.tester.id)
        try:
            await self.vc.delete()
        except Exception:
            pass

@bot.tree.command(name="stafftest", description="Start Staff Evaluation Test")
@app_commands.describe(candidate="User to test")
async def stafftest(interaction: discord.Interaction, candidate: discord.Member):
    if not has_perm(interaction.user, bot.cfg, "Staff-Test"):
        return await interaction.response.send_message("No permission", ephemeral=True)

    vc = await interaction.guild.create_voice_channel(f"Test - {candidate.display_name}")
    view = TestControl(bot.cfg, interaction.user, candidate, vc)
    await interaction.response.send_message("Test Panel", view=view, ephemeral=True)

# ----------------- Level role handling (improved) -----------------
async def update_level_roles(guild: discord.Guild, member: discord.Member, cfg: dict):

    levels = cfg.get("levels", {}) or {}
    level_items = []
    for rid_str, w in levels.items():
        try:
            r = guild.get_role(int(rid_str))
            if r:
                level_items.append((r, int(w)))
        except Exception:
            continue
    level_items.sort(key=lambda x: x[1], reverse=True)

    to_remove = [r for r, _ in level_items if r in member.roles]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Recalculating level roles")
        except Exception:
            pass

    return

def get_member_top_level_weight(member: discord.Member, cfg: dict) -> int:
    """Return the highest weight (int) among member's roles from cfg['levels'], or 0."""
    levels = cfg.get("levels", {}) or {}
    best = 0
    for rid_str, w in levels.items():
        try:
            rid = int(rid_str)
        except Exception:
            continue
        if any(r.id == rid for r in member.roles):
            try:
                if int(w) > best:
                    best = int(w)
            except Exception:
                pass
    return best

def get_roles_sorted_by_weight(guild: discord.Guild, cfg: dict, ascending: bool = True) -> List[discord.Role]:
    levels = cfg.get("levels", {}) or {}
    pairs = []
    for rid_str, w in levels.items():
        try:
            role = guild.get_role(int(rid_str))
            if role:
                pairs.append((role, int(w)))
        except Exception:
            continue
    pairs.sort(key=lambda x: x[1], reverse=not ascending)
    return [p[0] for p in pairs]

def _parse_levels(cfg: dict) -> List[Tuple[str, int]]:
    levels = cfg.get("levels", {}) or {}
    parsed = []
    for rid, w in levels.items():
        try:
            parsed.append((str(rid), int(w)))
        except Exception:
            continue
    parsed.sort(key=lambda x: x[1])
    return parsed


def _member_current_level(member: discord.Member, cfg: dict) -> Tuple[Optional[str], Optional[int]]:
    levels = _parse_levels(cfg)
    if not levels:
        return None, None

    role_ids = {rid for rid, _ in levels}
    member_cfg_roles = []

    for role in member.roles:
        if not role:
            continue
        rid = str(role.id)
        if rid in role_ids:
            for cfg_rid, w in levels:
                if cfg_rid == rid:
                    member_cfg_roles.append((cfg_rid, w))
                    break

    if not member_cfg_roles:
        return None, None

    return max(member_cfg_roles, key=lambda x: x[1])


def _configured_role_objects(guild: discord.Guild, cfg: dict) -> List[discord.Role]:
    levels = _parse_levels(cfg)
    roles = []
    for rid, _ in levels:
        role = guild.get_role(int(rid))
        if role:
            roles.append(role)
    return roles


@bot.tree.command(name="promote", description="Promote a member to the next level role")
@app_commands.describe(member="Member to promote")
async def promote(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction.user, bot.cfg, "Staff-Promote"):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    cfg = bot.cfg
    levels = _parse_levels(cfg)
    if not levels:
        return await interaction.response.send_message("No configured level roles.", ephemeral=True)

    _, cur_weight = _member_current_level(member, cfg)
    if cur_weight is None:
        cur_weight = float("-inf")

    candidates = [(rid, w) for rid, w in levels if w > cur_weight]
    if not candidates:
        return await interaction.response.send_message("Already at highest level.", ephemeral=True)

    next_rid, next_weight = min(candidates, key=lambda x: x[1])
    target_role = interaction.guild.get_role(int(next_rid))
    if not target_role:
        return await interaction.response.send_message("Target role not found.", ephemeral=True)

    to_remove = [r for r in _configured_role_objects(interaction.guild, cfg) if r in member.roles]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Promoted by {interaction.user}")
    except Exception:
        pass

    try:
        await member.add_roles(target_role, reason=f"Promoted by {interaction.user}")
    except Exception:
        return await interaction.response.send_message("Failed to add role.", ephemeral=True)

    await update_level_roles(interaction.guild, member, cfg)
    await add_history(cfg, member.id, f"Promoted to {target_role.name}", interaction.user.id)

    await save_config(cfg)
    bot.cfg = cfg

    await interaction.response.send_message(f"{member.mention} promoted to {target_role.mention}", ephemeral=True)


@bot.tree.command(name="demote", description="Demote a member to the previous level role")
@app_commands.describe(member="Member to demote")
async def demote(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction.user, bot.cfg, "Staff-Demote"):
        return await interaction.response.send_message("No permission.", ephemeral=True)

    cfg = bot.cfg
    levels = _parse_levels(cfg)
    if not levels:
        return await interaction.response.send_message("No configured level roles.", ephemeral=True)

    cur_rid, cur_weight = _member_current_level(member, cfg)
    if cur_weight is None:
        return await interaction.response.send_message("Member has no configured level role.", ephemeral=True)

    lower = [(rid, w) for rid, w in levels if w < cur_weight]
    if not lower:
        return await interaction.response.send_message("Already at lowest level.", ephemeral=True)

    prev_rid, prev_weight = max(lower, key=lambda x: x[1])
    target_role = interaction.guild.get_role(int(prev_rid))
    if not target_role:
        return await interaction.response.send_message("Target role not found.", ephemeral=True)

    to_remove = [r for r in _configured_role_objects(interaction.guild, cfg) if r in member.roles]

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Demoted by {interaction.user}")
    except Exception:
        pass

    try:
        await member.add_roles(target_role, reason=f"Demoted by {interaction.user}")
    except Exception:
        return await interaction.response.send_message("Failed to add role.", ephemeral=True)

    await update_level_roles(interaction.guild, member, cfg)
    await add_history(cfg, member.id, f"Demoted to {target_role.name}", interaction.user.id)

    await save_config(cfg)
    bot.cfg = cfg

    await interaction.response.send_message(f"{member.mention} demoted to {target_role.mention}", ephemeral=True)
@bot.tree.command(name="removestaff", description="Remove a member from staff (confirmation required)")
@app_commands.describe(member="Member to remove from staff")
async def removestaff(interaction: discord.Interaction, member: discord.Member):
    if not has_perm(interaction.user, bot.cfg, "Staff-Remove"):
        return await interaction.response.send_message("No permission", ephemeral=True)

    cfg = bot.cfg
    # Determine if target is staff: must have at least one role from permissions["Staff-Promote"]
    staff_role_ids = cfg.get("permissions", {}).get("Staff-Promote", [])
    is_staff = False
    member_staff_roles = []
    for rid in staff_role_ids:
        try:
            role = interaction.guild.get_role(int(rid))
        except Exception:
            role = None
        if role and role in member.roles:
            is_staff = True
            member_staff_roles.append(role)

    if not is_staff:
        return await interaction.response.send_message(f"{member.mention} is not a staff member (no matching roles).", ephemeral=True)

    default_role_id = cfg.get("settings", {}).get("default_role")
    default_role_obj = None
    if default_role_id:
        try:
            default_role_obj = interaction.guild.get_role(int(default_role_id))
        except Exception:
            default_role_obj = None

    extra = ""
    if default_role_obj and default_role_obj in member_staff_roles:
        extra = "\n\n⚠️ Note: This member currently has the default staff role (assigned after passing test)."

    view = ConfirmRemoveStaffView(cfg, interaction.user.id, member)
    await interaction.response.send_message(f"🔩 Are You Sure you wanna remove this Staff?\nMember: {member.mention}{extra}", view=view, ephemeral=True)

@bot.tree.command(name="remove", description="(alias) Remove Staff Roles (with confirmation)")
@app_commands.describe(member="Member to remove from staff")
async def remove_alias(interaction: discord.Interaction, member: discord.Member):
    # Reuse removestaff logic
    await removestaff.callback(interaction, member)

# ----------------- LOA handling (robust) -----------------
@tasks.loop(minutes=1)
async def check_loa():
    now = datetime.utcnow()
    cfg = bot.cfg
    to_remove = []

    for user_id, data in list(cfg.get("loa_tasks", {}).items()):
        if not isinstance(data, dict) or "end" not in data or "guild" not in data:
            to_remove.append(user_id)
            continue
        try:
            end_ts = datetime.fromisoformat(data["end"])
        except Exception:
            to_remove.append(user_id)
            continue
        guild = bot.get_guild(data["guild"])
        if not guild:
            to_remove.append(user_id)
            continue
        try:
            member = guild.get_member(int(user_id))
        except Exception:
            member = None
        if member and now >= end_ts:
            prev_nick = cfg.get("prev_nicks", {}).get(str(user_id))
            if prev_nick:
                try:
                    await member.edit(nick=prev_nick)
                except Exception:
                    pass
            to_remove.append(user_id)

    for uid in to_remove:
        cfg.get("loa_tasks", {}).pop(uid, None)
        cfg.get("prev_nicks", {}).pop(uid, None)

    await save_config(cfg)
    bot.cfg = cfg

@bot.tree.command(name="loa", description="Give LOA to a member")
@app_commands.describe(member="Member to give LOA", duration="Duration (e.g., 1m, 2h, 3d)")
async def loa(interaction: discord.Interaction, member: discord.Member, duration: str):
    if not has_perm(interaction.user, bot.cfg, "Staff-LOA"):
        return await interaction.response.send_message("You don't have permission", ephemeral=True)

    match = re.match(r"^(\d+)([mhd])$", duration)
    if not match:
        return await interaction.response.send_message("Invalid duration format! Use 1m / 2h / 3d", ephemeral=True)

    value, unit = int(match.group(1)), match.group(2)
    delta = None
    if unit == "m":
        delta = timedelta(minutes=value)
    elif unit == "h":
        delta = timedelta(hours=value)
    elif unit == "d":
        delta = timedelta(days=value)

    end_time = datetime.utcnow() + delta

    prev_nick = member.nick or member.name
    bot.cfg["prev_nicks"][str(member.id)] = prev_nick

    current_nick = member.nick or member.name
    if not current_nick.startswith("[LOA]"):
        new_nick = f"[LOA] {current_nick}"
    else:
        new_nick = current_nick

    try:
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        return await interaction.response.send_message("Bot lacks permission to change nickname", ephemeral=True)

    bot.cfg.setdefault("loa_tasks", {})[str(member.id)] = {
        "end": end_time.isoformat(),
        "guild": interaction.guild.id
    }
    await save_config(bot.cfg)

    await interaction.response.send_message(f"{member.mention} is now on LOA until {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}", ephemeral=True)

# ----------------- Reset to defaults (owner-only) -----------------
class ConfirmResetView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.result = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        # Reset config file
        await save_config(DEFAULT_CONFIG.copy())
        bot.cfg = DEFAULT_CONFIG.copy()
        self.result = True
        await interaction.response.edit_message(content="Configuration has been reset to defaults.", view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.result = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

@bot.tree.command(name="resetfact", description="Reset all settings to defaults (owner only)")
async def resetfact(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        return await interaction.response.send_message("Only the server owner can run this command.", ephemeral=True)
    view = ConfirmResetView(interaction.user.id)
    await interaction.response.send_message("⛏️ Are you sure you wanna reset All settings?", view=view, ephemeral=True)

# ----------------- Misc previously present commands kept -----------------
@bot.tree.command(name="permgui", description="Permission Management Panel")
async def permgui(interaction: discord.Interaction):
    if not has_perm(interaction.user, bot.cfg, "Config-GUI"):
        return await interaction.response.send_message("You don't have permission", ephemeral=True)
    view = PermGUIView(bot.cfg, interaction.user.id)
    await interaction.response.send_message("Permission Manager", view=view, ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)
