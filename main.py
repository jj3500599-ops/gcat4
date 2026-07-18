import discord
from discord.ext import commands, tasks
import json
import os
from datetime import datetime, timedelta
import string
import random
import hashlib
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import threading
import logging

load_dotenv()

# ==================== CONFIG ====================
TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.environ.get('PORT', 3000))
DATABASE_FILE = 'keys_database.json'
ADMIN_ROLE = "운영진"
KEY_LENGTH = 32

# Logging 설정 (Render에서 확인 가능)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Intents 설정
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Discord Bot
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask 앱
app = Flask(__name__)

# ==================== DATABASE FUNCTIONS ====================

def load_database():
    """데이터베이스 로드"""
    if os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"users": {}, "keys": {}, "hwids": {}, "temp_keys": {}}

def save_database(data):
    """데이터베이스 저장"""
    with open(DATABASE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_key():
    """고유한 KEY 생성 (32자리)"""
    characters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    key = ''.join(random.choice(characters) for _ in range(KEY_LENGTH))
    return key

def get_expiry_date():
    """일주일 뒤 만료 날짜 반환"""
    return (datetime.now() + timedelta(days=7)).isoformat()

def validate_hwid(hwid: str):
    """HWID 형식 검증"""
    if not hwid or len(hwid) < 8:
        return False
    return True

# ==================== FLASK API ROUTES ====================

@app.route('/verify', methods=['GET'])
def verify_key():
    """
    Roblox 게임에서 KEY 검증 요청
    사용법: GET /verify?key=KEY_VALUE
    응답: { "success": true/false }
    """
    try:
        key = request.args.get('key', '').strip()
        
        if not key:
            logger.warning("[API] 비어있는 KEY 검증 요청")
            return jsonify({"success": False, "message": "KEY가 비어있습니다"}), 400
        
        db = load_database()
        
        # 임시 키 확인 (게임에서 한 번만 사용 가능)
        if key in db.get("temp_keys", {}):
            temp_key_data = db["temp_keys"][key]
            
            # 만료 시간 확인
            expiry_time = datetime.fromisoformat(temp_key_data["expires_at"])
            if datetime.now() > expiry_time:
                del db["temp_keys"][key]
                save_database(db)
                logger.info(f"[API] 만료된 임시 KEY 검증 실패: {key[:10]}...")
                return jsonify({"success": False, "message": "만료된 KEY입니다"}), 403
            
            # HWID 확인
            provided_hwid = request.args.get('hwid', '').strip()
            if provided_hwid != temp_key_data["hwid"]:
                logger.warning(f"[API] HWID 불일치: {provided_hwid} != {temp_key_data['hwid']}")
                return jsonify({"success": False, "message": "HWID가 일치하지 않습니다"}), 403
            
            # KEY 검증 성공 - 임시 키 삭제
            user_id = temp_key_data["user_id"]
            del db["temp_keys"][key]
            save_database(db)
            
            logger.info(f"[API] KEY 검증 성공: {key[:10]}... (사용자: {user_id})")
            return jsonify({
                "success": True, 
                "message": "KEY 검증 완료",
                "user_id": user_id
            }), 200
        
        # 정규 KEY 확인
        if key in db.get("keys", {}):
            key_data = db["keys"][key]
            
            # 만료 시간 확인
            expiry_time = datetime.fromisoformat(key_data["expires_at"])
            if datetime.now() > expiry_time:
                logger.info(f"[API] 만료된 KEY 검증 실패: {key[:10]}...")
                return jsonify({"success": False, "message": "만료된 KEY입니다"}), 403
            
            # HWID 확인
            provided_hwid = request.args.get('hwid', '').strip()
            if provided_hwid != key_data["hwid"]:
                logger.warning(f"[API] HWID 불일치: {provided_hwid} != {key_data['hwid']}")
                return jsonify({"success": False, "message": "HWID가 일치하지 않습니다"}), 403
            
            logger.info(f"[API] KEY 검증 성공: {key[:10]}...")
            return jsonify({
                "success": True,
                "message": "KEY 유효",
                "user_id": key_data["user_id"],
                "expires_at": key_data["expires_at"]
            }), 200
        
        logger.warning(f"[API] 존재하지 않는 KEY 검증: {key[:10]}...")
        return jsonify({"success": False, "message": "유효하지 않은 KEY입니다"}), 404
    
    except Exception as e:
        logger.error(f"[API] 오류 발생: {str(e)}")
        return jsonify({"success": False, "message": f"서버 오류: {str(e)}"}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Render 헬스 체크 (서버 유지용)"""
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()}), 200

@app.route('/', methods=['GET'])
def home():
    """홈페이지"""
    return jsonify({
        "bot": "Gcat HUB KEY System",
        "version": "1.0",
        "status": "running",
        "endpoints": {
            "health": "/health",
            "verify": "/verify?key=KEY_VALUE&hwid=HWID_VALUE"
        }
    }), 200

# ==================== DISCORD EVENTS ====================

@bot.event
async def on_ready():
    """봇이 준비되면 실행"""
    logger.info(f'{bot.user} 로그인 완료')
    key_expiry_check.start()
    await bot.change_presence(activity=discord.Game(name="!key 명령어를 입력하세요"))
    logger.info(f"[봇] 디스코드 봇 준비 완료")

# ==================== DISCORD COMMANDS ====================

@bot.command(name='key', help='KEY 시스템 관련 명령어')
async def key_system(ctx):
    """KEY 시스템 메인 명령어"""
    db = load_database()
    user_id = str(ctx.author.id)
    
    embed = discord.Embed(
        title="🔐 KEY 시스템",
        description="아래 명령어를 클릭하여 작업을 수행하세요.",
        color=discord.Color.blue()
    )
    embed.add_field(name="📋 사용 가능한 명령어", value=
        "• `!key_request <HWID>` - 새로운 KEY 발급\n"
        "• `!key_renew` - KEY 재발급 (일주일마다)\n"
        "• `!key_info` - 현재 KEY 정보 조회\n"
        "• `!key_status <KEY>` - KEY 유효성 검증\n"
        "• `!key_admin_list` - 모든 KEY 목록 (관리자)\n"
        "• `!key_admin_remove <USER_ID>` - KEY 삭제 (관리자)",
        inline=False
    )
    embed.set_footer(text="Gcat HUB Key System | 모든 KEY는 발급 후 7일간 유효합니다")
    
    await ctx.send(embed=embed)

@bot.command(name='key_request', help='새로운 KEY 발급받기')
async def key_request(ctx, hwid: str = None):
    """새로운 KEY 발급"""
    
    if hwid is None:
        embed = discord.Embed(
            title="❌ 오류",
            description="사용법: `!key_request <HWID>`\n\nHWID를 입력하지 않았습니다.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    if not validate_hwid(hwid):
        embed = discord.Embed(
            title="❌ 오류",
            description="HWID가 유효하지 않습니다.\n최소 8자 이상이어야 합니다.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    db = load_database()
    user_id = str(ctx.author.id)
    
    # 이미 KEY가 있는지 확인
    if user_id in db["users"]:
        embed = discord.Embed(
            title="⚠️ 경고",
            description="이미 발급받은 KEY가 있습니다.\n\n`!key_info` 명령어로 현재 KEY를 확인하세요.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return
    
    # 같은 HWID로 이미 발급된 KEY가 있는지 확인
    if hwid in db["hwids"]:
        embed = discord.Embed(
            title="⚠️ 경고",
            description=f"이 HWID는 이미 등록되어 있습니다.\n사용자: <@{db['hwids'][hwid]['user_id']}>",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return
    
    # 새 KEY 생성
    new_key = generate_key()
    expiry_date = get_expiry_date()
    
    # 데이터베이스에 저장
    db["users"][user_id] = {
        "username": ctx.author.name,
        "user_tag": str(ctx.author),
        "key": new_key,
        "hwid": hwid,
        "created_at": datetime.now().isoformat(),
        "expires_at": expiry_date,
        "renewal_count": 0
    }
    
    db["hwids"][hwid] = {
        "user_id": user_id,
        "key": new_key
    }
    
    db["keys"][new_key] = {
        "user_id": user_id,
        "hwid": hwid,
        "created_at": datetime.now().isoformat(),
        "expires_at": expiry_date
    }
    
    save_database(db)
    
    # 임베드 메시지
    embed = discord.Embed(
        title="✅ KEY 발급 완료",
        description="새로운 KEY가 발급되었습니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="🔑 KEY", value=f"`{new_key}`", inline=False)
    embed.add_field(name="🖥️ HWID", value=f"`{hwid}`", inline=False)
    embed.add_field(name="📅 생성일", value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.add_field(name="⏰ 만료일", value=datetime.fromisoformat(expiry_date).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.set_footer(text="KEY는 7일 후 만료됩니다. 재발급은 !key_renew 명령어로 받으세요.")
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 발급] 사용자: {ctx.author} ({user_id}) | HWID: {hwid}")

@bot.command(name='key_renew', help='KEY 재발급받기 (7일마다)')
async def key_renew(ctx):
    """KEY 재발급"""
    db = load_database()
    user_id = str(ctx.author.id)
    
    # 사용자가 KEY를 가지고 있는지 확인
    if user_id not in db["users"]:
        embed = discord.Embed(
            title="❌ 오류",
            description="발급받은 KEY가 없습니다.\n`!key_request <HWID>` 명령어로 새 KEY를 발급받으세요.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    user_data = db["users"][user_id]
    old_key = user_data["key"]
    expiry_time = datetime.fromisoformat(user_data["expires_at"])
    current_time = datetime.now()
    
    # 재발급 가능 여부 확인 (만료 3일 전부터 가능)
    days_until_expiry = (expiry_time - current_time).days
    
    if days_until_expiry > 3:
        embed = discord.Embed(
            title="⏳ 아직 재발급할 수 없습니다",
            description=f"KEY 만료까지 **{days_until_expiry}일** 남았습니다.\n\n만료 **3일 전부터** 재발급이 가능합니다.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return
    
    # 새 KEY 생성
    new_key = generate_key()
    new_expiry = get_expiry_date()
    
    # 구 KEY 데이터 삭제
    if old_key in db["keys"]:
        del db["keys"][old_key]
    
    # 새 KEY 데이터 저장
    user_data["key"] = new_key
    user_data["expires_at"] = new_expiry
    user_data["renewal_count"] = user_data.get("renewal_count", 0) + 1
    user_data["last_renewed_at"] = datetime.now().isoformat()
    
    db["hwids"][user_data["hwid"]]["key"] = new_key
    
    db["keys"][new_key] = {
        "user_id": user_id,
        "hwid": user_data["hwid"],
        "created_at": datetime.now().isoformat(),
        "expires_at": new_expiry
    }
    
    save_database(db)
    
    # 임베드 메시지
    embed = discord.Embed(
        title="✅ KEY 재발급 완료",
        description="새로운 KEY가 발급되었습니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="🔑 새 KEY", value=f"`{new_key}`", inline=False)
    embed.add_field(name="🔑 구 KEY", value=f"`{old_key}`", inline=False)
    embed.add_field(name="⏰ 새로운 만료일", value=datetime.fromisoformat(new_expiry).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.add_field(name="📊 재발급 횟수", value=str(user_data["renewal_count"]), inline=False)
    embed.set_footer(text="구 KEY는 더 이상 사용할 수 없습니다.")
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 재발급] 사용자: {ctx.author} ({user_id}) | 재발급 횟수: {user_data['renewal_count']}")

@bot.command(name='key_info', help='현재 KEY 정보 조회')
async def key_info(ctx):
    """사용자의 KEY 정보 조회"""
    db = load_database()
    user_id = str(ctx.author.id)
    
    if user_id not in db["users"]:
        embed = discord.Embed(
            title="❌ KEY를 찾을 수 없음",
            description="아직 발급받은 KEY가 없습니다.\n`!key_request <HWID>` 명령어로 KEY를 발급받으세요.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    user_data = db["users"][user_id]
    expiry_time = datetime.fromisoformat(user_data["expires_at"])
    current_time = datetime.now()
    days_until_expiry = (expiry_time - current_time).days
    
    # 상태 판정
    if days_until_expiry < 0:
        status = "❌ 만료됨"
        color = discord.Color.red()
    elif days_until_expiry <= 3:
        status = "⚠️ 곧 만료됨 (재발급 가능)"
        color = discord.Color.orange()
    else:
        status = "✅ 활성화됨"
        color = discord.Color.green()
    
    embed = discord.Embed(
        title="📋 KEY 정보",
        description=status,
        color=color
    )
    embed.add_field(name="🔑 KEY", value=f"`{user_data['key']}`", inline=False)
    embed.add_field(name="🖥️ HWID", value=f"`{user_data['hwid']}`", inline=False)
    embed.add_field(name="👤 사용자", value=user_data['user_tag'], inline=False)
    embed.add_field(name="📅 발급일", value=datetime.fromisoformat(user_data['created_at']).strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="⏰ 만료일", value=expiry_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="📊 남은 일수", value=f"{max(0, days_until_expiry)}일", inline=True)
    embed.add_field(name="🔄 재발급 횟수", value=str(user_data.get('renewal_count', 0)), inline=False)
    
    if "last_renewed_at" in user_data:
        embed.add_field(name="🕐 마지막 재발급", value=datetime.fromisoformat(user_data['last_renewed_at']).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    
    embed.set_footer(text="KEY 정보는 개인이므로 공개하지 마세요.")
    
    await ctx.send(embed=embed)

@bot.command(name='key_status', help='KEY 유효성 검증')
async def key_status(ctx, key: str = None):
    """KEY 유효성 검증"""
    
    if key is None:
        embed = discord.Embed(
            title="❌ 오류",
            description="사용법: `!key_status <KEY>`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    db = load_database()
    
    if key not in db["keys"]:
        embed = discord.Embed(
            title="❌ 유효하지 않은 KEY",
            description="등록되지 않은 KEY입니다.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    key_data = db["keys"][key]
    expiry_time = datetime.fromisoformat(key_data["expires_at"])
    current_time = datetime.now()
    
    if current_time > expiry_time:
        embed = discord.Embed(
            title="❌ 만료된 KEY",
            description="이 KEY는 만료되었습니다.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    days_remaining = (expiry_time - current_time).days
    
    embed = discord.Embed(
        title="✅ 유효한 KEY",
        description="이 KEY는 유효합니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="🖥️ HWID", value=f"`{key_data['hwid']}`", inline=False)
    embed.add_field(name="⏰ 남은 시간", value=f"{days_remaining}일", inline=False)
    embed.set_footer(text="KEY는 일주일마다 재발급이 필요합니다.")
    
    await ctx.send(embed=embed)

# ==================== ADMIN COMMANDS ====================

@bot.command(name='key_admin_list', help='모든 KEY 목록 (관리자)')
@commands.has_role(ADMIN_ROLE)
async def key_admin_list(ctx, page: int = 1):
    """모든 KEY 목록 조회 (페이지네이션)"""
    db = load_database()
    users = list(db["users"].items())
    
    if not users:
        embed = discord.Embed(
            title="📋 KEY 목록",
            description="발급된 KEY가 없습니다.",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        return
    
    # 페이지네이션 (페이지당 10개)
    items_per_page = 10
    total_pages = (len(users) + items_per_page - 1) // items_per_page
    
    if page < 1 or page > total_pages:
        await ctx.send(f"❌ 유효하지 않은 페이지입니다. (1-{total_pages})")
        return
    
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_users = users[start_idx:end_idx]
    
    embed = discord.Embed(
        title=f"📋 KEY 목록 ({page}/{total_pages})",
        description=f"총 {len(users)}개의 KEY",
        color=discord.Color.blue()
    )
    
    for user_id, user_data in page_users:
        expiry_time = datetime.fromisoformat(user_data["expires_at"])
        current_time = datetime.now()
        days_remaining = (expiry_time - current_time).days
        
        status = "✅" if days_remaining > 0 else "❌"
        
        embed.add_field(
            name=f"{status} {user_data['user_tag']}",
            value=f"KEY: `{user_data['key'][:16]}...`\nHWID: `{user_data['hwid']}`\n만료: {days_remaining}일",
            inline=False
        )
    
    embed.set_footer(text=f"페이지 {page}/{total_pages} | !key_admin_list <페이지번호>")
    await ctx.send(embed=embed)

@bot.command(name='key_admin_remove', help='KEY 삭제 (관리자)')
@commands.has_role(ADMIN_ROLE)
async def key_admin_remove(ctx, user_id: str = None):
    """특정 사용자의 KEY 삭제"""
    
    if user_id is None:
        embed = discord.Embed(
            title="❌ 오류",
            description="사용법: `!key_admin_remove <USER_ID>`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    db = load_database()
    
    if user_id not in db["users"]:
        embed = discord.Embed(
            title="❌ 사용자를 찾을 수 없음",
            description=f"ID: {user_id}",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    user_data = db["users"][user_id]
    old_key = user_data["key"]
    hwid = user_data["hwid"]
    
    # 데이터 삭제
    del db["users"][user_id]
    if hwid in db["hwids"]:
        del db["hwids"][hwid]
    if old_key in db["keys"]:
        del db["keys"][old_key]
    
    save_database(db)
    
    embed = discord.Embed(
        title="✅ KEY 삭제 완료",
        description=f"사용자 KEY가 삭제되었습니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="👤 사용자", value=user_data['user_tag'], inline=False)
    embed.add_field(name="🔑 삭제된 KEY", value=f"`{old_key}`", inline=False)
    embed.add_field(name="🖥️ HWID", value=f"`{hwid}`", inline=False)
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 삭제] 관리자: {ctx.author} | 대상: {user_data['user_tag']} ({user_id})")

@bot.command(name='key_admin_stats', help='KEY 시스템 통계 (관리자)')
@commands.has_role(ADMIN_ROLE)
async def key_admin_stats(ctx):
    """KEY 시스템 통계"""
    db = load_database()
    
    total_keys = len(db["users"])
    total_hwids = len(db["hwids"])
    
    # 활성화된 KEY 개수
    active_keys = 0
    expired_keys = 0
    expiring_soon = 0
    
    current_time = datetime.now()
    
    for user_data in db["users"].values():
        expiry_time = datetime.fromisoformat(user_data["expires_at"])
        days_remaining = (expiry_time - current_time).days
        
        if days_remaining < 0:
            expired_keys += 1
        elif days_remaining <= 3:
            expiring_soon += 1
        else:
            active_keys += 1
    
    # 재발급 횟수 통계
    total_renewals = sum(user.get('renewal_count', 0) for user in db["users"].values())
    avg_renewals = total_renewals / total_keys if total_keys > 0 else 0
    
    embed = discord.Embed(
        title="📊 KEY 시스템 통계",
        color=discord.Color.blue()
    )
    embed.add_field(name="📈 총 KEY 수", value=str(total_keys), inline=True)
    embed.add_field(name="🖥️ 총 HWID 수", value=str(total_hwids), inline=True)
    embed.add_field(name="✅ 활성화된 KEY", value=str(active_keys), inline=True)
    embed.add_field(name="⚠️ 곧 만료될 KEY", value=str(expiring_soon), inline=True)
    embed.add_field(name="❌ 만료된 KEY", value=str(expired_keys), inline=True)
    embed.add_field(name="🔄 총 재발급 횟수", value=str(total_renewals), inline=True)
    embed.add_field(name="📊 평균 재발급 횟수", value=f"{avg_renewals:.2f}", inline=False)
    
    embed.set_footer(text="Gcat HUB Key System | 실시간 통계")
    
    await ctx.send(embed=embed)

# ==================== BACKGROUND TASKS ====================

@tasks.loop(hours=24)
async def key_expiry_check():
    """매일 만료 예정 KEY 체크 및 알림"""
    db = load_database()
    current_time = datetime.now()
    
    expiring_users = []
    
    for user_id, user_data in db["users"].items():
        expiry_time = datetime.fromisoformat(user_data["expires_at"])
        days_remaining = (expiry_time - current_time).days
        
        # 3일 전일 때만 알림
        if days_remaining == 3:
            expiring_users.append((user_id, user_data, days_remaining))
    
    # 만료 예정 사용자에게 DM 발송
    for user_id, user_data, days_remaining in expiring_users:
        try:
            user = await bot.fetch_user(int(user_id))
            embed = discord.Embed(
                title="⏰ KEY 재발급 알림",
                description=f"당신의 KEY가 곧 만료됩니다!",
                color=discord.Color.orange()
            )
            embed.add_field(name="🔑 KEY", value=f"`{user_data['key'][:16]}...`", inline=False)
            embed.add_field(name="⏰ 남은 일수", value=f"{days_remaining}일", inline=False)
            embed.add_field(name="📌 조치", value="`!key_renew` 명령어로 KEY를 재발급받으세요.", inline=False)
            
            await user.send(embed=embed)
            logger.info(f"[알림 발송] {user_data['user_tag']} - 만료 {days_remaining}일 전")
        except Exception as e:
            logger.error(f"[알림 실패] {user_id}: {e}")

# ==================== FLASK & DISCORD BOT RUN ====================

def run_discord_bot():
    """Discord 봇을 별도 스레드에서 실행"""
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"[봇 오류] Discord 봇 실행 중 오류: {e}")

def run_flask():
    """Flask 서버 실행"""
    try:
        logger.info(f"[Flask] 포트 {PORT}에서 Flask 서버 시작")
        app.run(host='0.0.0.0', port=PORT, debug=False)
    except Exception as e:
        logger.error(f"[Flask 오류] Flask 서버 실행 중 오류: {e}")

if __name__ == "__main__":
    if not TOKEN:
        logger.error("❌ DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        exit()
    
    logger.info("=" * 50)
    logger.info("🚀 Gcat HUB KEY System 시작")
    logger.info(f"📌 Flask 포트: {PORT}")
    logger.info(f"📌 Discord 봇: 준비 중...")
    logger.info("=" * 50)
    
    # Flask는 메인 스레드에서 실행
    # Discord 봇은 별도 스레드에서 실행
    bot_thread = threading.Thread(target=run_discord_bot, daemon=True)
    bot_thread.start()
    
    # Flask 서버 실행 (메인 스레드)
    run_flask()
