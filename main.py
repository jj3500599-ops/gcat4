import sys
import os

try:
    sys.modules['audioop'] = type(sys)('audioop')
except Exception:
    pass

import discord
from discord.ext import commands, tasks
import json
from datetime import datetime, timedelta, timezone
import string
import random
import hashlib
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import threading
import logging
import secrets

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.environ.get('PORT', 10000))
DATABASE_FILE = 'keys_database.json'
ADMIN_ROLE = "운영진"
KEY_LENGTH = 32

db_lock = threading.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
app = Flask(__name__)

def get_now_utc():
    return datetime.now(timezone.utc)

def parse_iso_datetime(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return get_now_utc() - timedelta(days=365)

def validate_hwid_format(hwid: str) -> bool:
    if not hwid or len(hwid) < 16:
        return False
    if not all(c.isalnum() or c in '-_' for c in hwid):
        return False
    return True

def calculate_hwid_hash(hwid: str) -> str:
    return hashlib.sha256(hwid.encode()).hexdigest()

def verify_hwid_match(provided_hwid: str, stored_hwid_hash: str) -> bool:
    return calculate_hwid_hash(provided_hwid) == stored_hwid_hash

def generate_key():
    characters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    return ''.join(secrets.choice(characters) for _ in range(KEY_LENGTH))

def get_expiry_date():
    return (get_now_utc() + timedelta(days=7)).isoformat()
def load_database():
    with db_lock:
        if os.path.exists(DATABASE_FILE):
            try:
                with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "hwid_hashes" not in data: data["hwid_hashes"] = {}
                    if "temp_keys" not in data: data["temp_keys"] = {}
                    return data
            except Exception as e:
                logger.error(f"[DB] 로드 오류: {e}")
                return {"users": {}, "keys": {}, "hwids": {}, "hwid_hashes": {}, "temp_keys": {}}
        return {"users": {}, "keys": {}, "hwids": {}, "hwid_hashes": {}, "temp_keys": {}}

def save_database(data):
    with db_lock:
        temp_file = DATABASE_FILE + '.tmp'
        try:
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, DATABASE_FILE)
        except Exception as e:
            logger.error(f"[DB] 저장 오류: {e}")
            if os.path.exists(temp_file): os.remove(temp_file)

@app.route('/verify', methods=['GET'])
def verify_key():
    try:
        key = request.args.get('key', '').strip()
        provided_hwid = request.args.get('hwid', '').strip()
        
        if not key or not provided_hwid:
            return jsonify({"success": False, "message": "KEY와 HWID가 필수입니다"}), 200
        
        if not validate_hwid_format(provided_hwid):
            return jsonify({"success": False, "message": "유효하지 않은 HWID 형식입니다 (16자 이상, 영문/숫자/하이픈만)"}), 200
        
        db = load_database()
        current_time = get_now_utc()
        
        if key in db.get("keys", {}):
            key_data = db["keys"][key]
            expiry_time = parse_iso_datetime(key_data["expires_at"])
            
            if current_time > expiry_time:
                return jsonify({"success": False, "message": "만료된 KEY입니다. !key_renew로 재발급받으세요"}), 200
            
            stored_hwid_hash = key_data.get("hwid_hash")
            if not stored_hwid_hash or not verify_hwid_match(provided_hwid, stored_hwid_hash):
                return jsonify({"success": False, "message": "이 KEY는 다른 기기에 등록되어 있습니다"}), 200
            
            return jsonify({"success": True, "message": "KEY 검증 완료", "user_id": key_data["user_id"], "hwid_verified": True}), 200
        
        if key in db.get("temp_keys", {}):
            temp_key_data = db["temp_keys"][key]
            expiry_time = parse_iso_datetime(temp_key_data["expires_at"])
            
            if current_time > expiry_time:
                db_write = load_database()
                if key in db_write.get("temp_keys", {}):
                    del db_write["temp_keys"][key]
                    save_database(db_write)
                return jsonify({"success": False, "message": "만료된 KEY입니다"}), 200
            
            stored_hwid_hash = temp_key_data.get("hwid_hash")
            if not verify_hwid_match(provided_hwid, stored_hwid_hash):
                return jsonify({"success": False, "message": "이 KEY는 다른 기기에 등록되어 있습니다"}), 200
            
            user_id = temp_key_data["user_id"]
            db_write = load_database()
            if key in db_write.get("temp_keys", {}):
                del db_write["temp_keys"][key]
                save_database(db_write)
            return jsonify({"success": True, "message": "임시 KEY 검증 완료 (일회용)", "user_id": user_id, "hwid_verified": True, "temporary": True}), 200
        
        return jsonify({"success": False, "message": "유효하지 않은 KEY입니다"}), 200
    except Exception as e:
        logger.error(f"[API] 오류 발생: {str(e)}")
        return jsonify({"success": False, "message": "서버 오류"}), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "alive", "timestamp": get_now_utc().isoformat()}), 200

@app.route('/', methods=['GET'])
def home():
    return jsonify({"bot": "Gcat HUB KEY System", "version": "2.0", "status": "running"}), 200
@bot.event
async def on_ready():
    logger.info(f'{bot.user} 로그인 완료')
    if not key_expiry_check.is_running():
        key_expiry_check.start()
    await bot.change_presence(activity=discord.Game(name="!key 명령어를 입력하세요"))

@bot.command(name='key')
async def key_system(ctx):
    embed = discord.Embed(title="🔐 KEY 시스템 (HWID 검증)", description="기기별 독립 KEY 시스템 - 공유 및 리셀 불가능", color=discord.Color.blue())
    embed.add_field(name="📋 사용 가능한 명령어", value="• `!key_request <HWID>` - 새로운 KEY 발급\n• `!key_renew` - KEY 재발급 (일주일마다)\n• `!key_info` - 현재 KEY 정보 조회\n• `!key_status <KEY>` - KEY 유효성 검증\n• `!key_admin_list` - 모든 KEY 목록 (관리자)\n• `!key_admin_remove <USER_ID>` - KEY 삭제 (관리자)\n• `!key_admin_stats` - 시스템 통계 (관리자)", inline=False)
    embed.add_field(name="🖥️ HWID란?", value="기기의 고유 식별자입니다. 본인 기기에서만 작동하며 공유가 불가능합니다.", inline=False)
    embed.set_footer(text="Gcat HUB Key System v2.0 | 무결점 마스터 보안판")
    await ctx.send(embed=embed)

@bot.command(name='key_request')
async def key_request(ctx, hwid: str = None):
    if hwid is None:
        await ctx.send("❌ 사용법: `!key_request <HWID>`")
        return
    if not validate_hwid_format(hwid):
        await ctx.send("❌ 형식이 맞지 않습니다. (16자 이상, 영문/숫자/하이픈만 사용 가능)")
        return
    
    db = load_database()
    user_id = str(ctx.author.id)
    hwid_hash = calculate_hwid_hash(hwid)
    
    if user_id in db["users"]:
        await ctx.send("⚠️ 이미 KEY가 발급되었습니다. `!key_info`로 조회하세요.")
        return
    if hwid_hash in db.get("hwid_hashes", {}):
        await ctx.send("🚫 이 기기는 이미 다른 계정에 귀속되어 무단 공유가 차단되었습니다.")
        return
    
    new_key = generate_key()
    expiry_date = get_expiry_date()
    
    db["users"][user_id] = {"username": ctx.author.name, "user_tag": str(ctx.author), "key": new_key, "hwid_hash": hwid_hash, "hwid_display": hwid[:8] + "..." + hwid[-8:], "created_at": get_now_utc().isoformat(), "expires_at": expiry_date, "renewal_count": 0}
    db["hwid_hashes"][hwid_hash] = user_id
    db["keys"][new_key] = {"user_id": user_id, "hwid_hash": hwid_hash, "created_at": get_now_utc().isoformat(), "expires_at": expiry_date}
    save_database(db)
    
    embed = discord.Embed(title="✅ KEY 발급 완료", color=discord.Color.green())
    embed.add_field(name="🔑 KEY", value=f"`{new_key}`", inline=False)
    embed.add_field(name="🖥️ HWID", value=f"`{hwid[:8]}...{hwid[-8:]}` (해시됨)", inline=False)
    embed.add_field(name="⏰ 만료일", value=parse_iso_datetime(expiry_date).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    await ctx.send(embed=embed)

@bot.command(name='key_renew')
async def key_renew(ctx):
    db = load_database()
    user_id = str(ctx.author.id)
    if user_id not in db["users"]:
        await ctx.send("❌ 발급받은 KEY가 없습니다.")
        return
    user_data = db["users"][user_id]
    old_key = user_data["key"]
    hwid_hash = user_data["hwid_hash"]
    
    is_admin = False
    if isinstance(ctx.author, discord.Member):
        for role in ctx.author.roles:
            if role.name == ADMIN_ROLE:
                is_admin = True
                break
                
    if not is_admin:
        expiry_time = parse_iso_datetime(user_data["expires_at"])
        days_until_expiry = (expiry_time - get_now_utc()).days
        if days_until_expiry > 3:
            await ctx.send(f"⏳ 아직 재발급할 수 없습니다. ({days_until_expiry}일 남음)")
            return
    
    new_key = generate_key()
    new_expiry = get_expiry_date()
    
    if old_key in db["keys"]: del db["keys"][old_key]
    user_data["key"] = new_key
    user_data["expires_at"] = new_expiry
    user_data["renewal_count"] = user_data.get("renewal_count", 0) + 1
    user_data["last_renewed_at"] = get_now_utc().isoformat()
    
    db["keys"][new_key] = {"user_id": user_id, "hwid_hash": hwid_hash, "created_at": get_now_utc().isoformat(), "expires_at": new_expiry}
    save_database(db)
    
    msg_title = "👑 관리자 권한 강제 KEY 재발급 완료" if is_admin else "✅ KEY 재발급 완료"
    await ctx.send(embed=discord.Embed(title=msg_title, description=f"새 KEY: `{new_key}`\n기존 제한을 완전히 우회하여 즉시 갱신되었습니다.", color=discord.Color.green()))

@bot.command(name='key_info')
async def key_info(ctx):
    db = load_database()
    user_id = str(ctx.author.id)
    if user_id not in db["users"]:
        await ctx.send("❌ KEY가 없습니다.")
        return
    user_data = db["users"][user_id]
    expiry_time = parse_iso_datetime(user_data["expires_at"])
    await ctx.send(f"📋 **내 KEY 정보**\n🔑 KEY: `{user_data['key']}`\n⏰ 만료일: `{expiry_time.strftime('%Y-%m-%d %H:%M:%S')}`")

@bot.command(name='key_status')
async def key_status(ctx, key: str = None):
    if key is None: return
    db = load_database()
    if key not in db["keys"]:
        await ctx.send("❌ 유효하지 않은 KEY")
        return
    key_data = db["keys"][key]
    expiry_time = parse_iso_datetime(key_data["expires_at"])
    if get_now_utc() > expiry_time:
        await ctx.send("❌ 만료된 KEY")
    else:
        await ctx.send("✅ 유효한 KEY입니다.")

@bot.command(name='key_admin_list')
@commands.has_role(ADMIN_ROLE)
async def key_admin_list(ctx, page: int = 1):
    db = load_database()
    users = list(db["users"].items())
    if not users: return
    items_per_page = 10
    total_pages = (len(users) + items_per_page - 1) // items_per_page
    page_users = users[(page-1)*items_per_page : page*items_per_page]
    
    embed = discord.Embed(title=f"📋 KEY 목록 ({page}/{total_pages})", color=discord.Color.blue())
    for user_id, user_data in page_users:
        expiry_time = parse_iso_datetime(user_data["expires_at"])
        days_remaining = (expiry_time - get_now_utc()).days
        status = "✅" if days_remaining > 0 else "❌"
        embed.add_field(name=f"{status} {user_data['user_tag']}", value=f"KEY: `{user_data['key'][:16]}...`", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='key_admin_remove')
@commands.has_role(ADMIN_ROLE)
async def key_admin_remove(ctx, user_id: str = None):
    if user_id is None: return
    db = load_database()
    if user_id not in db["users"]: return
    user_data = db["users"][user_id]
    old_key = user_data["key"]
    hwid_hash = user_data["hwid_hash"]
    
    del db["users"][user_id]
    if hwid_hash in db.get("hwid_hashes", {}): del db["hwid_hashes"][hwid_hash]
    if old_key in db["keys"]: del db["keys"][old_key]
    save_database(db)
    await ctx.send(f"✅ {user_data['user_tag']} KEY 삭제 성공")

@bot.command(name='key_admin_stats')
@commands.has_role(ADMIN_ROLE)
async def key_admin_stats(ctx):
    db = load_database()
    total_keys = len(db["users"])
    embed = discord.Embed(title="📊 KEY 시스템 통계", color=discord.Color.blue())
    embed.add_field(name="📈 총 KEY 수", value=str(total_keys), inline=True)
    await ctx.send(embed=embed)

@tasks.loop(hours=24)
async def key_expiry_check():
    db = load_database()
    current_time = get_now_utc()
    for user_id, user_data in db["users"].items():
        expiry_time = parse_iso_datetime(user_data["expires_at"])
        if (expiry_time - current_time).days == 3:
            try:
                user = await bot.fetch_user(int(user_id))
                await user.send(f"⏰ **Gcat HUB 만료 알림**: KEY가 3일 뒤 만료됩니다. `!key_renew`로 갱신하세요!")
            except Exception:
                pass

def run_discord_bot():
    try: bot.run(TOKEN)
    except Exception as e: logger.error(f"[봇 오류] {e}")

if __name__ == "__main__":
    if not TOKEN: exit()
    threading.Thread(target=run_discord_bot, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
