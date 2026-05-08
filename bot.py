import os
import json
import base64
import discord
import aiohttp
import datetime
import random
from pathlib import Path
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True  
intents.guilds = True
# --- Configuration ---
OLLAMA_API_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:31b-cloud" 
REMINDER_CHANNEL_ID = 1501833920449351720 

# Set reminder time (e.g., 8:00 AM UTC). 
REMINDER_TIME = datetime.time(hour=8, minute=0) 

class TradingAssistant(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        # Start the background reminder loop when the bot boots
        self.daily_reminder.start()

    @tasks.loop(time=REMINDER_TIME)
    async def daily_reminder(self):
        # --- 1. Load Habits & Mistakes ---
        insights_path = Path.cwd() / "master_insights.json"
        good_list, bad_list = [], []
        
        if insights_path.exists():
            with open(insights_path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                    full_good = data.get("good_habits", [])
                    full_bad = data.get("mistakes", [])
                    good_list = random.sample(full_good, min(3, len(full_good)))
                    bad_list = random.sample(full_bad, min(3, len(full_bad)))
                except: pass

        # --- 2. Load a Random Knowledge Article ---
        kb_dir = Path.cwd() / "knowledge_base"
        knowledge_snippet = "No articles found yet. Run `!extract_knowledge` first!"
        article_title = "Strategy Spotlight"

        if kb_dir.exists():
            articles = list(kb_dir.glob("*.md"))
            if articles:
                selected_file = random.choice(articles)
                with open(selected_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Extract the Title (usually the first line starting with #)
                    lines = content.splitlines()
                    for line in lines:
                        if line.startswith("# "):
                            article_title = line.replace("# ", "").strip()
                            break
                    # Extract the first 300 characters of the overview
                    # (Skipping the title line)
                    body_text = "\n".join([l for l in lines if not l.startswith("#") and l.strip()])
                    knowledge_snippet = body_text[:400] + "..." if len(body_text) > 400 else body_text

        # --- 3. Build the Mega-Embed ---
        embed = discord.Embed(
            title="☀️ Morning Trading Prep & Study", 
            description=f"Time to sharpen the axe. Here is your focus for {datetime.date.today().strftime('%B %d')}:",
            color=discord.Color.gold()
        )
        
        # Section A: Personal Insights
        if good_list:
            embed.add_field(name="✅ Strengths to Maintain", value="\n".join([f"• {x}" for x in good_list]), inline=False)
        if bad_list:
            embed.add_field(name="⚠️ Mistakes to Watch For", value="\n".join([f"• {x}" for x in bad_list]), inline=False)
        
        # Section B: Strategy Study (The Knowledge Article)
        embed.add_field(
            name=f"📖 Today's Study Topic: {article_title}", 
            value=f"```markdown\n{knowledge_snippet}\n```\n*Read the full file in `/knowledge_base/`*", 
            inline=False
        )
        
        channel = self.get_channel(REMINDER_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)
        insights_path = Path.cwd() / "master_insights.json"
        
        if not insights_path.exists():
            return 

        with open(insights_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return

        full_good_list = data.get("good_habits", [])
        full_bad_list = data.get("mistakes", [])

        if not full_good_list and not full_bad_list:
            return

        # Randomly select 3 items from the massive master lists
        good_list = random.sample(full_good_list, min(3, len(full_good_list)))
        bad_list = random.sample(full_bad_list, min(3, len(full_bad_list)))

        embed = discord.Embed(
            title="☀️ Morning Trading Prep", 
            description="Here are 3 random insights pulled from your historical journal to focus on today:",
            color=discord.Color.gold()
        )
        
        if good_list:
            embed.add_field(name="✅ Strengths to Maintain", value="\n".join([f"• {x}" for x in good_list]), inline=False)
        if bad_list:
            embed.add_field(name="⚠️ Mistakes to Watch For", value="\n".join([f"• {x}" for x in bad_list]), inline=False)
        
        channel = self.get_channel(REMINDER_CHANNEL_ID)
        if channel:
            await channel.send(embed=embed)

    @daily_reminder.before_loop
    async def before_reminder(self):
        await self.wait_until_ready()

bot = TradingAssistant()

@bot.event
async def on_ready():
    print(f'Successfully logged in as {bot.user}!')
    print('Ready to receive commands.')
    print('--------------------------')

@bot.command()
async def ping(ctx):
    await ctx.send('Pong! 🏓')

@bot.command()
async def list_channels(ctx):
    """Prints all channels the bot currently has permission to see."""
    accessible = [c.name for c in ctx.guild.text_channels]
    print(f"--- ACCESSIBLE CHANNELS ({len(accessible)}) ---")
    for name in accessible:
        print(f" - {name}")
    await ctx.send(f"I can currently see **{len(accessible)}** channels. Check your Python terminal for the list!")

@bot.command()
async def morning_prep(ctx):
    """Manually trigger the morning reminder with 3 random insights."""
    await ctx.send("🎲 Shuffling your trade history and picking 3 lessons...")
    
    # This calls the exact same logic used for the 8:00 AM reminder
    await bot.daily_reminder()
# --- 1. The Sync Command ---
@bot.command()
async def extract_knowledge(ctx, *, channel_input: str = None):
    """
    Synthesizes a channel's history AND screenshots into a Knowledge Article.
    """
    target_channel = None
    if channel_input:
        try:
            target_channel = await commands.TextChannelConverter().convert(ctx, channel_input)
        except commands.ChannelNotFound:
            target_channel = discord.utils.get(ctx.guild.text_channels, name=channel_input)
    else:
        target_channel = ctx.channel

    if not target_channel:
        await ctx.send(f"❌ I couldn't find channel `{channel_input}`. Check permissions!")
        return

    await ctx.send(f"📚 Gathering text and screenshots from **#{target_channel.name}**...")

    full_text_log = []
    images_b64 = []
    IMAGE_LIMIT = 10 # We limit to 10 key screenshots to avoid 400 errors

    async for message in target_channel.history(limit=500, oldest_first=True):
        # 1. Collect Text
        if message.content and not message.author.bot:
            timestamp = message.created_at.strftime("%Y-%m-%d")
            full_text_log.append(f"[{timestamp}] {message.author.name}: {message.content}")
        
        # 2. Collect Screenshots (up to the limit)
        if message.attachments and len(images_b64) < IMAGE_LIMIT:
            for attachment in message.attachments:
                if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                    img_bytes = await attachment.read()
                    images_b64.append(base64.b64encode(img_bytes).decode("utf-8"))

    if not full_text_log and not images_b64:
        await ctx.send("❌ No content found to analyze.")
        return

    combined_content = "\n".join(full_text_log)

    # 3. Enhanced Vision Prompt
    prompt = f"""
    You are a Knowledge Management Expert specializing in Technical Trading Systems.
    I am providing you with chat history and screenshots from the channel '#{target_channel.name}'.
    
    TASK:
    1. Analyze the text AND the attached screenshots (charts/diagrams).
    2. Extract core trading logic, rules, and 'If/Then' scenarios visible in the images.
    3. Synthesize everything into a structured 'Knowledge Article' in Markdown.
    
    STRUCTURE:
    - # [Title: Strategy/Concept Name]
    - ## Summary: What is the core purpose of this concept?
    - ## Visual Identifiers: Describe exactly what the trader should look for on a chart (based on the screenshots).
    - ## Rules of Engagement: Step-by-step logic for using this knowledge.
    - ## Common Pitfalls: What to avoid (invalid setups).

    DATA:
    {combined_content}
    """

    # 4. API Call
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "images": images_b64,
        "stream": False
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(OLLAMA_API_URL, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    article_content = result["response"]

                    kb_dir = Path.cwd() / "knowledge_base"
                    kb_dir.mkdir(exist_ok=True)
                    
                    file_name = f"{target_channel.name}_article.md"
                    with open(kb_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(article_content)

                    embed = discord.Embed(title=f"📖 Knowledge Synthesized: #{target_channel.name}", color=discord.Color.blue())
                    embed.add_field(name="Screenshots Processed", value=str(len(images_b64)))
                    embed.add_field(name="Output File", value=f"`/knowledge_base/{file_name}`")
                    await ctx.send(embed=embed)
                else:
                    # Capture verbose error if the payload is still too big
                    err_msg = await response.text()
                    await ctx.send(f"❌ Ollama Error {response.status}: {err_msg[:200]}")
        except Exception as e:
            await ctx.send(f"❌ Error: {str(e)}")
@bot.command()
async def sync_fractals(ctx):
    target_category_name = "Fractal_Trades"
    category = discord.utils.get(ctx.guild.categories, name=target_category_name)
    
    if not category:
        await ctx.send(f"Couldn't find category '{target_category_name}'.")
        return

    await ctx.send(f"Starting flat JSON sync for **{target_category_name}**...")
    base_dir = Path.cwd() / "downloads" / target_category_name

    for channel in category.text_channels:
        channel_path = base_dir / channel.name
        channel_path.mkdir(parents=True, exist_ok=True)
        channel_data = []

        async for message in channel.history(limit=None, oldest_first=True):
            if message.content or message.attachments:
                msg_entry = {
                    "message_id": str(message.id),
                    "timestamp": message.created_at.isoformat(),
                    "author": message.author.name,
                    "content": message.content,
                    "images": []
                }

                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                        unique_filename = f"{message.id}_{attachment.filename}"
                        file_path = channel_path / unique_filename
                        await attachment.save(file_path)
                        msg_entry["images"].append(unique_filename)

                channel_data.append(msg_entry)

        with open(channel_path / "history.json", "w", encoding="utf-8") as f:
            json.dump(channel_data, f, indent=4)

    await ctx.send("✅ Sync complete! Run `!analyze_trades` to process any new entries.")

# --- 2. The Trade-by-Trade Analysis Command ---
@bot.command()
async def analyze_trades(ctx):
    base_dir = Path.cwd() / "downloads" / "Fractal_Trades"
    master_file = Path.cwd() / "master_insights.json"
    tracker_file = Path.cwd() / "last_analyzed.txt"
    
    # 1. Get the last processed message ID safely
    last_id = 0
    if tracker_file.exists():
        try:
            with open(tracker_file, "r") as f:
                content = f.read().strip()
                if content.isdigit():
                    last_id = int(content)
        except Exception:
            pass

    # 2. Gather NEW trades across all channels
    new_trades = []
    if base_dir.exists():
        for channel_dir in base_dir.iterdir():
            if channel_dir.is_dir():
                history_file = channel_dir / "history.json"
                if history_file.exists():
                    with open(history_file, "r", encoding="utf-8") as f:
                        channel_data = json.load(f)
                        for entry in channel_data:
                            if int(entry["message_id"]) > last_id:
                                entry["_folder_path"] = str(channel_dir)
                                new_trades.append(entry)

    if not new_trades:
        await ctx.send("✅ No new trades found. Master Insights is up to date!")
        return

    # 3. Load current Master Insights safely
    current_master = {"good_habits": [], "mistakes": []}
    if master_file.exists():
        try:
            with open(master_file, "r", encoding="utf-8") as f:
                current_master = json.load(f)
        except json.JSONDecodeError:
            print("Warning: master_insights.json is empty or corrupt. Starting fresh.")

    status_message = await ctx.send(f"🧠 Found {len(new_trades)} new trade(s). Analyzing them one by one...")
    highest_id = last_id
    successful_analyses = 0

    # 4. Process ONE trade at a time sequentially
    async with aiohttp.ClientSession() as session:
        for idx, trade in enumerate(new_trades, 1):
            trade_id = int(trade["message_id"])
            if trade_id > highest_id:
                highest_id = trade_id
                
            # Grab images for THIS specific trade only (caps payload size)
            images_b64 = []
            for img_name in trade["images"]:
                img_path = Path(trade["_folder_path"]) / img_name
                if img_path.exists():
                    with open(img_path, "rb") as img_file:
                        images_b64.append(base64.b64encode(img_file.read()).decode("utf-8"))

            prompt = f"""
            Analyze this single trading journal entry and its attached chart(s).
            The trader utilizes the TTrades Fractal Model (SMT, CISD, FVG).

            Trade Notes:
            {trade['content']}

            TASK: 
            Extract the specific 'good_habits' and 'mistakes' mentioned or visible in THIS SPECIFIC trade.
            Keep the insights concise and actionable.
            If there are no mistakes, leave the mistakes array empty. If no good habits, leave it empty.
            
            Respond STRICTLY in valid JSON format:
            {{"good_habits": ["..."], "mistakes": ["..."]}}
            """

            payload = {
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "images": images_b64,
                "stream": False
            }

            try:
                async with session.post(OLLAMA_API_URL, json=payload) as response:
                    if response.status == 200:
                        result = await response.json()
                        raw_text = result["response"]
                        
                        # Clean up formatting in case Ollama wraps the JSON in Markdown ticks
                        cleaned_text = raw_text.replace("```json", "").replace("```", "").strip()
                        
                        parsed_data = json.loads(cleaned_text)
                        
                        # Append the new findings directly to our Python dictionary
                        current_master["good_habits"].extend(parsed_data.get("good_habits", []))
                        current_master["mistakes"].extend(parsed_data.get("mistakes", []))
                        
                        successful_analyses += 1
                        
                        # Update Discord status periodically to show progress
                        if idx % 2 == 0 or idx == len(new_trades):
                            await status_message.edit(content=f"🧠 Analyzing trades... ({idx}/{len(new_trades)} completed)")

                    else:
                        print(f"Trade {trade_id} failed with status: {response.status}")
            except Exception as e:
                print(f"Error parsing trade {trade_id}: {e}")

    # 5. Deduplicate the lists so we don't save the exact same bullet point 10 times
    current_master["good_habits"] = list(set(current_master["good_habits"]))
    current_master["mistakes"] = list(set(current_master["mistakes"]))

    # 6. Save the newly expanded Master Insights
    with open(master_file, "w", encoding="utf-8") as f:
        json.dump(current_master, f, indent=4)
        
    with open(tracker_file, "w") as f:
        f.write(str(highest_id))

    # 7. Send final confirmation embed
    embed = discord.Embed(
        title="✅ Analysis Complete", 
        description=f"Successfully analyzed {successful_analyses} trade(s).",
        color=discord.Color.green()
    )
    embed.add_field(name="Total Strengths in DB", value=str(len(current_master["good_habits"])), inline=True)
    embed.add_field(name="Total Mistakes in DB", value=str(len(current_master["mistakes"])), inline=True)
    await ctx.send(embed=embed)

# --- START THE BOT ---
bot_token = os.getenv('BOT_KEY')

if not bot_token:
    print("❌ ERROR: BOT_KEY is missing! Check your .env file.")
    exit()

bot.run(bot_token)