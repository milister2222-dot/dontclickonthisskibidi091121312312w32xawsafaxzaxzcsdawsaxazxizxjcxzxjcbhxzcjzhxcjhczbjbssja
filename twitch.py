import json
import os
import random
import asyncio
import aiohttp
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict

try:
    import discord
    from discord import app_commands
    from discord.ext import commands
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False

TOKEN = "TOKEN_HERE"
ALLOWED_CHANNEL_ID = 1492484129114292285  
STOCK_UPDATE_INTERVAL = 30  

_OWNER_ID_ENC = "MTQ1ODgzNTg0MTkwMzIzNTIwNQ=="
OWNER_ID = int(__import__('base64').b64decode(_OWNER_ID_ENC).decode())

ROLE_REWARDS = {
    1491132571738833039: (30, 60),
    1492531877515497662: (50, 60),
    1492839713130807447: (70, 120),
    1493009758494658763: (70, 60),
    1491128051201740910: (50, 45),
    1491117935169900680: (50, 30),
    1493009934097584220: (100, 60),
    1492835201074597980: (80, 120),
    1493010049428099163: (125, 60),
    1491117935182348422: (150, 120),
    1493010171683668130: (150, 60),
    1491753932006096936: (150, 120),
    1493010480925507784: (200, 60),
    1492835576179593308: (200, 120),
    1491117935169900681: (400, 60),
    1491117935182348429: (400, 120),
    1491117935182348430: (750, 0),
}

EXCLUSIVE_ROLE = 1491117935169900682
DEFAULT_FOLLOWERS = 30
DEFAULT_COOLDOWN = 60

last_used = {}
user_queues: Dict[int, deque] = {}
queue_lock = asyncio.Lock()

GQL_HEADERS = {
    "Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

print_lock = asyncio.Lock()


@dataclass
class QueueItem:
    user_id: int
    username: str
    target_username: str
    target_id: str
    follow_amount: int
    interaction: discord.Interaction
    added_at: float = field(default_factory=time.time)
    status: str = "pending"
    success: int = 0
    errors: int = 0


def _file_lines(filename):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as f:
        return [l.strip() for l in f if l.strip()]


def _load_tokens():
    return _file_lines("tokens.txt")


def _load_proxies():
    lines = _file_lines("proxies.txt")
    out = []
    for l in lines:
        if "://" not in l:
            l = f"http://{l}"
        out.append(l)
    return out


def parse_proxy(proxy_str):
    if not proxy_str:
        return None
    if proxy_str.startswith(("http://", "https://")):
        return proxy_str
    return f"http://{proxy_str}"


async def get_twitch_user(username, proxies=None):
    payload = json.dumps([{
        "operationName": "GetIDFromLogin",
        "variables": {"login": username},
        "extensions": {"persistedQuery": {"version": 1,
            "sha256Hash": "94e82a7b1e3c21e186daa73ee2afc4b8f23bade1fbbff6fe8ac133f50a2f58ca"}}
    }])

    proxy_list = []
    if proxies:
        proxy_list = [parse_proxy(p) for p in proxies if p]

    for attempt in range(3):
        for proxy in [None] + random.sample(proxy_list, min(3, len(proxy_list))):
            try:
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15)) as s:
                    async with s.post("https://gql.twitch.tv/gql",
                                      headers=GQL_HEADERS, data=payload, proxy=proxy) as r:
                        if r.status == 200:
                            d = await r.json()
                            if isinstance(d, list) and d:
                                user = d[0].get("data", {}).get("user")
                            elif isinstance(d, dict):
                                user = d.get("data", {}).get("user")
                            else:
                                user = None
                            if user and user.get("id"):
                                return user["id"]
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return None


async def _execute_follow(target_id, token, proxy_url=None, proxies=None):
    headers = {
        "Accept": "application/json",
        "Authorization": f"OAuth {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    payload = json.dumps([{
        "operationName": "FollowUserMutation",
        "variables": {"targetId": str(target_id), "disableNotifications": False},
        "extensions": {"persistedQuery": {"version": 1,
            "sha256Hash": "cd112d9483ede85fa0da514a5657141c24396efbc7bac0ea3623e839206573b8"}}
    }])

    proxy_attempts = []
    if not proxies:
        proxy_attempts = [None]
    elif len(proxies) == 1:
        proxy_attempts = [proxies[0], None]
    else:
        if proxy_url:
            proxy_attempts = [proxy_url]
        for _ in range(min(3, len(proxies))):
            p = random.choice(proxies)
            if p not in proxy_attempts:
                proxy_attempts.append(p)
        proxy_attempts.append(None)

    for proxy in proxy_attempts:
        try:
            timeout = aiohttp.ClientTimeout(total=8, connect=2, sock_read=4)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post("https://gql.twitch.tv/gql",
                                       data=payload, headers=headers, proxy=proxy) as response:
                    result_text = await response.text()

                    if response.status in (200, 204):
                        if "errors" not in result_text.lower():
                            return "ok"
                        if "already following" in result_text.lower():
                            return "already_following"
                        if "too many follows" in result_text.lower():
                            return "TOO_MANY_FOLLOWS"
                    continue
        except asyncio.TimeoutError:
            continue
        except Exception:
            continue
    return False


async def run_follow_engine(twitch_id, target_count, tokens, proxies, on_progress=None):
    base_workers = 100 if target_count <= 30 else (200 if target_count <= 100 else 300)

    if proxies and len(proxies) > 0:
        max_workers = min(base_workers, len(proxies) * 50)
    else:
        max_workers = min(base_workers, 100)

    async with print_lock:
        print(f"[Started] Target: {target_count} | Workers: {max_workers} | Proxies: {len(proxies) if proxies else 0}")

    lock = asyncio.Lock()
    used_tokens = set()
    blocked_tokens = set()
    stats = {"completed": 0, "errors": 0}
    stop_event = asyncio.Event()

    token_pool = list(tokens)
    random.shuffle(token_pool)
    token_index = [0]

    async def _worker():
        while not stop_event.is_set():
            async with lock:
                if stats["completed"] >= target_count:
                    stop_event.set()
                    return
                token = None
                while token_index[0] < len(token_pool):
                    candidate = token_pool[token_index[0]]
                    token_index[0] += 1
                    if candidate not in used_tokens and candidate not in blocked_tokens:
                        used_tokens.add(candidate)
                        token = candidate
                        break
                if token is None:
                    return

            if stop_event.is_set():
                return

            proxy_url = random.choice(proxies) if proxies else None
            result = await _execute_follow(twitch_id, token, proxy_url, proxies)

            async with lock:
                if result == "ok":
                    if stats["completed"] < target_count:
                        stats["completed"] += 1
                        current = stats["completed"]
                        async with print_lock:
                            print(f"{GREEN}[FOLLOWED] {current}/{target_count}{RESET}")
                        if on_progress:
                            await on_progress(stats["completed"], stats["errors"])
                        if stats["completed"] >= target_count:
                            stop_event.set()
                elif result in ("TOO_MANY_FOLLOWS",):
                    blocked_tokens.add(token)
                    used_tokens.discard(token)
                else:
                    stats["errors"] += 1
                    if stats["errors"] <= 3:
                        async with print_lock:
                            print(f"{RED}[ERROR] Follow failed ({result}){RESET}")

                if stats["errors"] >= 30 and stats["completed"] == 0:
                    async with print_lock:
                        print(f"{RED}[Engine] Stopping - too many errors{RESET}")
                    stop_event.set()
                    return

            await asyncio.sleep(random.uniform(0.001, 0.01))

    workers = min(max_workers, target_count, len(tokens))
    await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(workers)],
                         return_exceptions=True)

    return stats["completed"], stats["errors"]


def get_user_limits(member):
    highest_followers = DEFAULT_FOLLOWERS
    lowest_cooldown = DEFAULT_COOLDOWN
    is_exclusive = False

    for role in member.roles:
        if role.id == EXCLUSIVE_ROLE:
            is_exclusive = True
        if role.id in ROLE_REWARDS:
            followers, cooldown = ROLE_REWARDS[role.id]
            if followers > highest_followers:
                highest_followers = followers
            if cooldown < lowest_cooldown:
                lowest_cooldown = cooldown

    return highest_followers, lowest_cooldown, is_exclusive


if DISCORD_AVAILABLE:
    class MyBot(commands.Bot):
        def __init__(self):
            intents = discord.Intents.default()
            intents.message_content = True
            super().__init__(command_prefix=".", intents=intents)
            self.queue_task = None
            self.current_job = None

        async def setup_hook(self):
            pass

    def check_channel(source):
        if ALLOWED_CHANNEL_ID == 0:
            return True
        channel_id = getattr(source, 'channel_id', None) or getattr(source, 'channel', None).id if hasattr(source, 'channel') else None
        return channel_id == ALLOWED_CHANNEL_ID

    bot = MyBot()

    def check_channel(source):
        if ALLOWED_CHANNEL_ID == 0:
            return True
        channel_id = getattr(source, 'channel_id', None) or getattr(source, 'channel', None).id if hasattr(source, 'channel') else None
        return channel_id == ALLOWED_CHANNEL_ID

    @bot.command(name="tfollow")
    async def tfollow(ctx: commands.Context, username: str = None):
        if not check_channel(ctx):
            embed = discord.Embed(
                title="WRONG CHANNEL",
                description="```This bot only works in the designated channel```",
                color=0xFF0000
            )
            return await ctx.send(embed=embed, delete_after=10)

        if not username:
            embed = discord.Embed(
                title="USAGE",
                description="```.tfollow <username>\nExample: .tfollow kaicenat```",
                color=0x9146FF
            )
            return await ctx.send(embed=embed, delete_after=30)

        member = ctx.author
        now = time.time()

        follow_amount, cooldown, is_exclusive = get_user_limits(member)

        if not is_exclusive and cooldown > 0:
            if member.id in last_used and now - last_used[member.id] < cooldown:
                remaining = int(cooldown - (now - last_used[member.id]))
                embed = discord.Embed(
                    title="COOLDOWN",
                    description=f"```Please wait {remaining} seconds```",
                    color=0xFF0000,
                    timestamp=discord.utils.utcnow()
                )
                embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/356/356001.png")
                return await ctx.send(embed=embed, delete_after=10)

        tokens = _load_tokens()
        proxies = _load_proxies()

        if not tokens:
            embed = discord.Embed(
                title="ERROR",
                description="```No tokens available```",
                color=0xFF0000,
                timestamp=discord.utils.utcnow()
            )
            return await ctx.send(embed=embed)

        target_username = username.strip().lower()

        
        search_embed = discord.Embed(
            title="SEARCHING",
            description=f"```Looking up {target_username}...```",
            color=0x9146FF,
            timestamp=discord.utils.utcnow()
        )
        status_msg = await ctx.send(embed=search_embed)

        target_id = await get_twitch_user(target_username, proxies)

        if not target_id:
            notfound_embed = discord.Embed(
                title="NOT FOUND",
                description=f"```User '{username}' not found on Twitch```",
                color=0xFF0000,
                timestamp=discord.utils.utcnow()
            )
            return await status_msg.edit(embed=notfound_embed)

        if not is_exclusive:
            last_used[member.id] = time.time()

       
        sending_embed = discord.Embed(
            title="SENDING FOLLOWERS",
            description=f"```Sending {follow_amount} followers to {target_username.upper()}...\nPlease wait```",
            color=0xFFAA00,
            timestamp=discord.utils.utcnow()
        )
        await status_msg.edit(embed=sending_embed)

      
        async def progress_callback(completed, errors):
            if completed % 100 == 0 or completed == follow_amount:
                progress_embed = discord.Embed(
                    title="SENDING FOLLOWERS",
                    description=f"```Sending {follow_amount} followers to {target_username.upper()}...\nProgress: {completed}/{follow_amount}```",
                    color=0xFFAA00,
                    timestamp=discord.utils.utcnow()
                )
                try:
                    await status_msg.edit(embed=progress_embed)
                except:
                    pass

       
        success, errors = await run_follow_engine(target_id, follow_amount, tokens, proxies, progress_callback)

       
        color = 0x00FF00 if success > 0 else 0xFFAA00

        complete_embed = discord.Embed(
            title="FOLLOWS COMPLETED",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        complete_embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/356/356001.png")
        complete_embed.add_field(name="TARGET", value=f"```{target_username.upper()}```", inline=False)
        complete_embed.add_field(name="SUCCESS", value=f"```{success}/{follow_amount}```", inline=True)
        complete_embed.add_field(name="ERRORS", value=f"```{errors}```", inline=True)
        complete_embed.add_field(name="STATUS", value=f"```Verified: {success > 0}```", inline=True)

        await status_msg.edit(embed=complete_embed)

@bot.command(name="tstock")
async def tstock(ctx: commands.Context):
    if not check_channel(ctx):
        embed = discord.Embed(
            title="WRONG CHANNEL",
            description="```This bot only works in the designated channel```",
            color=0xFF0000
        )
        return await ctx.send(embed=embed, delete_after=10)

    tokens = _load_tokens()
    token_count = len(tokens)

    embed = discord.Embed(
        title="STOCK STATUS",
        color=0x9146FF,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Available Tokens", value=f"```{token_count}```", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author.name}")

    await ctx.send(embed=embed, delete_after=30)

@bot.command(name="tqueue")
async def tqueue(ctx: commands.Context):
    if not check_channel(ctx):
        embed = discord.Embed(
            title="WRONG CHANNEL",
            description="```This bot only works in the designated channel```",
            color=0xFF0000
        )
        return await ctx.send(embed=embed, delete_after=10)

    user_id = ctx.author.id

    async with queue_lock:
        queue = user_queues.get(user_id, deque())
        queue_len = len(queue)
        current = queue[0] if queue_len > 0 else None

    if queue_len == 0:
        embed = discord.Embed(
            title="YOUR QUEUE",
            description="```No active requests in your queue```",
            color=0x9146FF,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/356/356001.png")
        embed.set_footer(text="Use .tfollow <username> to add a request")
        return await ctx.send(embed=embed, delete_after=30)

    
    embed = discord.Embed(
        title="YOUR QUEUE",
        description=f"You have {queue_len} request(s)",
        color=0x9146FF,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/356/356001.png")

    if current:
        progress = f"{current.success}/{current.follow_amount}" if current.status == "running" else "Waiting"
        status_text = f"{'Running' if current.status == 'running' else 'Pending'}"
        embed.add_field(
            name="CURRENT",
            value=f"```Target: {current.target_username.upper()}\nStatus: {status_text}\nProgress: {progress}```",
            inline=False
        )

    if queue_len > 1:
        embed.add_field(name="PENDING", value=f"```{queue_len - 1} job(s)```", inline=True)

    embed.add_field(name="POSITION", value=f"```#{1}/{queue_len}```", inline=True)
    embed.set_footer(text=f"Requested by {ctx.author.name}")

    await ctx.send(embed=embed)

@bot.command(name="thelp")
async def thelp(ctx: commands.Context):
    if not check_channel(ctx):
        embed = discord.Embed(
            title="WRONG CHANNEL",
            description="```This bot only works in the designated channel```",
            color=0xFF0000
        )
        return await ctx.send(embed=embed, delete_after=10)

    embed = discord.Embed(
        title="TWITCH FOLLOW BOT",
        description="Send Twitch followers to any channel",
        color=0x9146FF,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url="https://cdn-icons-png.flaticon.com/512/356/356001.png")

    embed.add_field(
        name="COMMANDS",
        value="```.tfollow <username> - Send followers to Twitch channel\n.tqueue            - Check your queue status\n.tstock            - View current stock\n.thelp             - Show this message```",
        inline=False
    )

    rewards_text = ""
    for role_id, (followers, cooldown) in sorted(ROLE_REWARDS.items(), key=lambda x: x[1][0]):
        role_name = {
            1491132571738833039: "Member",
            1491117935169900679: "Twitch Ads",
            1491128051201740910: "1x Boost",
            1491117935169900680: "2x Boost",
            1491117935169900681: "Premium",
            1491117935169900682: "Exclusive"
        }.get(role_id, f"Role {role_id}")
        cd_text = f"{cooldown}s" if cooldown > 0 else "No CD"
        rewards_text += f"{role_name}: {followers} ({cd_text})\n"

    embed.add_field(
        name="REWARDS (Follows / Cooldown)",
        value=f"```{rewards_text}```",
        inline=False
    )

    embed.set_footer(text="Arrowservices Bot")

    await ctx.send(embed=embed, delete_after=60)

@bot.event
async def on_ready():
    print(f"{GREEN}Bot logged in as {bot.user}{RESET}")
    print(f"{GREEN}Loaded {len(_load_tokens())} tokens{RESET}")
    print(f"{GREEN}Loaded {len(_load_proxies())} proxies{RESET}")


async def cli_main():
    print("=" * 50)
    print("  Twitch Follow Bot")
    print("=" * 50)
    print()

    username = input("Target username: ").strip().lower()
    if not username:
        print(f"{RED}[!] Username required{RESET}")
        return

    try:
        count = int(input("Number of follows: ").strip())
        if count <= 0:
            print(f"{RED}[!] Invalid number{RESET}")
            return
    except ValueError:
        print(f"{RED}[!] Invalid number{RESET}")
        return

    print()
    print(f"[+] Loading tokens and proxies...")
    tokens = _load_tokens()
    proxies = _load_proxies()

    print(f"[+] Tokens: {len(tokens)} | Proxies: {len(proxies)}")

    if not tokens:
        print(f"{RED}[!] No tokens found in tokens.txt{RESET}")
        return

    print(f"[+] Looking up user: {username}...")
    user_id = await get_twitch_user(username, proxies)

    if not user_id:
        print(f"{RED}[!] User '{username}' not found on Twitch{RESET}")
        return

    print(f"[+] User ID: {user_id}")
    print(f"[+] Starting follow operation...")
    print(f"[+] Target: {count} follows")
    print("-" * 50)

    start_time = time.time()

    def progress(s, e):
        pass

    success, errors = await run_follow_engine(user_id, count, tokens, proxies, on_progress=progress)

    elapsed = round(time.time() - start_time, 1)

    print("-" * 50)
    print(f"{GREEN}[+] Done!{RESET}")
    print(f"{GREEN}[+] Delivered: {success}/{count}{RESET}")
    print(f"{RED}[+] Errors: {errors}{RESET}")
    print(f"[+] Time: {elapsed}s")


if __name__ == "__main__":
    if DISCORD_AVAILABLE and TOKEN:
        try:
            bot.run(TOKEN)
        except KeyboardInterrupt:
            print(f"\n{RED}[!] Stopped by user{RESET}")
    else:
        try:
            asyncio.run(cli_main())
        except KeyboardInterrupt:
            print(f"\n{RED}[!] Stopped by user{RESET}")
