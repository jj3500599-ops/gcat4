import sys
import os

# audioop 오류 무시
sys.modules['audioop'] = None
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
import secrets

load_dotenv()

# ==================== CONFIG ====================
TOKEN = os.getenv('DISCORD_TOKEN')
PORT = int(os.environ.get('PORT', 3000))
DATABASE_FILE = 'keys_database.json'
ADMIN_ROLE = "운영진"
KEY_LENGTH = 32

# Logging 설정
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

# ==================== HWID 검증 함수 ====================

def validate_hwid_format(hwid: str) -> bool:
    """
    HWID 형식 엄격 검증
    - 최소 16자 이상 (기기 고유 식별자)
    - 영문, 숫자, 하이픈만 허용
    - 공백 불허
    """
    if not hwid or len(hwid) < 16:
        return False
    
    # 허용된 문자 체크 (영문, 숫자, 하이픈)
    if not all(c.isalnum() or c in '-_' for c in hwid):
        return False
    
    return True

def calculate_hwid_hash(hwid: str) -> str:
    """HWID의 SHA256 해시 생성 (저장용)"""
    return hashlib.sha256(hwid.encode()).hexdigest()

def verify_hwid_match(provided_hwid: str, stored_hwid_hash: str) -> bool:
    """제공된 HWID가 저장된 해시와 일치하는지 확인"""
    provided_hash = calculate_hwid_hash(provided_hwid)
    return provided_hash == stored_hwid_hash

def generate_key():
    """고유한 KEY 생성 (32자리)"""
    characters = string.ascii_uppercase + string.ascii_lowercase + string.digits
    key = ''.join(random.choice(characters) for _ in range(KEY_LENGTH))
    return key

def get_expiry_date():
    """일주일 뒤 만료 날짜 반환"""
    return (datetime.now() + timedelta(days=7)).isoformat()

# ==================== DATABASE FUNCTIONS ====================

def load_database():
    """데이터베이스 로드"""
    if os.path.exists(DATABASE_FILE):
        try:
            with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 기존 호환성 유지
                if "hwid_hashes" not in data:
                    data["hwid_hashes"] = {}
                if "temp_keys" not in data:
                    data["temp_keys"] = {}
                return data
        except Exception as e:
            logger.error(f"[DB] 로드 오류: {e}")
            return {"users": {}, "keys": {}, "hwids": {}, "hwid_hashes": {}, "temp_keys": {}}
    return {"users": {}, "keys": {}, "hwids": {}, "hwid_hashes": {}, "temp_keys": {}}

def save_database(data):
    """데이터베이스 저장"""
    try:
        with open(DATABASE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[DB] 저장 오류: {e}")

# ==================== FLASK API ROUTES ====================

@app.route('/verify', methods=['GET'])
def verify_key():
    """
    Roblox 게임에서 KEY 검증 + HWID 확인
    사용법: GET /verify?key=KEY_VALUE&hwid=DEVICE_HWID
    응답: { "success": true/false, "message": "...", "user_id": "..." }
    
    ⚠️ 핵심: 같은 KEY라도 다른 HWID에서는 실패!
    """
    try:
        key = request.args.get('key', '').strip()
        provided_hwid = request.args.get('hwid', '').strip()
        
        if not key or not provided_hwid:
            logger.warning("[API] 비어있는 KEY 또는 HWID")
            return jsonify({
                "success": False, 
                "message": "KEY와 HWID가 필수입니다"
            }), 400
        
        # HWID 형식 검증
        if not validate_hwid_format(provided_hwid):
            logger.warning(f"[API] 유효하지 않은 HWID 형식: {provided_hwid[:20]}...")
            return jsonify({
                "success": False,
                "message": "유효하지 않은 HWID 형식입니다 (16자 이상, 영문/숫자/하이픈만)"
            }), 400
        
        db = load_database()
        
        # 1. 정규 KEY 확인
        if key in db.get("keys", {}):
            key_data = db["keys"][key]
            
            # 만료 시간 확인
            expiry_time = datetime.fromisoformat(key_data["expires_at"])
            if datetime.now() > expiry_time:
                logger.warning(f"[API] 만료된 KEY: {key[:10]}...")
                return jsonify({
                    "success": False,
                    "message": "만료된 KEY입니다. !key_renew로 재발급받으세요"
                }), 403
            
            # ⭐ HWID 검증 (가장 중요한 부분)
            stored_hwid_hash = key_data.get("hwid_hash")
            if not stored_hwid_hash:
                logger.error(f"[API] HWID 해시 없음: {key[:10]}...")
                return jsonify({
                    "success": False,
                    "message": "KEY 정보 오류"
                }), 500
            
            if not verify_hwid_match(provided_hwid, stored_hwid_hash):
                logger.warning(f"[API] HWID 불일치 감지: KEY {key[:10]}... | 제공된 HWID: {provided_hwid[:20]}...")
                # 보안: 상세한 실패 정보는 제공 안함 (재판매 방지)
                return jsonify({
                    "success": False,
                    "message": "이 KEY는 다른 기기에 등록되어 있습니다"
                }), 403
            
            # ✅ 검증 성공
            logger.info(f"[API] KEY 검증 성공: {key[:10]}... | HWID: {provided_hwid[:20]}...")
            return jsonify({
                "success": True,
                "message": "KEY 검증 완료",
                "user_id": key_data["user_id"],
                "hwid_verified": True
            }), 200
        
        # 2. 임시 KEY 확인 (일회용)
        if key in db.get("temp_keys", {}):
            temp_key_data = db["temp_keys"][key]
            
            # 만료 시간 확인
            expiry_time = datetime.fromisoformat(temp_key_data["expires_at"])
            if datetime.now() > expiry_time:
                del db["temp_keys"][key]
                save_database(db)
                logger.warning(f"[API] 만료된 임시 KEY: {key[:10]}...")
                return jsonify({
                    "success": False,
                    "message": "만료된 KEY입니다"
                }), 403
            
            # ⭐ 임시 KEY도 HWID 검증
            stored_hwid_hash = temp_key_data.get("hwid_hash")
            if not verify_hwid_match(provided_hwid, stored_hwid_hash):
                logger.warning(f"[API] 임시 KEY HWID 불일치")
                return jsonify({
                    "success": False,
                    "message": "이 KEY는 다른 기기에 등록되어 있습니다"
                }), 403
            
            # ✅ 임시 KEY 검증 성공 - 즉시 삭제 (일회용)
            user_id = temp_key_data["user_id"]
            del db["temp_keys"][key]
            save_database(db)
            
            logger.info(f"[API] 임시 KEY 검증 성공 (일회용 삭제): {key[:10]}...")
            return jsonify({
                "success": True,
                "message": "임시 KEY 검증 완료 (일회용)",
                "user_id": user_id,
                "hwid_verified": True,
                "temporary": True
            }), 200
        
        logger.warning(f"[API] 존재하지 않는 KEY: {key[:10]}...")
        return jsonify({
            "success": False,
            "message": "유효하지 않은 KEY입니다"
        }), 404
    
    except Exception as e:
        logger.error(f"[API] 오류 발생: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"서버 오류"
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Render 헬스 체크 (서버 유지용)"""
    return jsonify({"status": "alive", "timestamp": datetime.now().isoformat()}), 200

@app.route('/', methods=['GET'])
def home():
    """홈페이지"""
    return jsonify({
        "bot": "Gcat HUB KEY System",
        "version": "2.0",
        "status": "running",
        "security": "HWID-Based Verification",
        "endpoints": {
            "health": "/health",
            "verify": "/verify?key=KEY_VALUE&hwid=DEVICE_HWID"
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
    embed = discord.Embed(
        title="🔐 KEY 시스템 (HWID 검증)",
        description="기기별 독립 KEY 시스템 - 다른 사람과 공유 불가능",
        color=discord.Color.blue()
    )
    embed.add_field(name="📋 사용 가능한 명령어", value=
        "• `!key_request <HWID>` - 새로운 KEY 발급\n"
        "• `!key_renew` - KEY 재발급 (일주일마다)\n"
        "• `!key_info` - 현재 KEY 정보 조회\n"
        "• `!key_status <KEY>` - KEY 유효성 검증\n"
        "• `!key_admin_list` - 모든 KEY 목록 (관리자)\n"
        "• `!key_admin_remove <USER_ID>` - KEY 삭제 (관리자)\n"
        "• `!key_admin_stats` - 시스템 통계 (관리자)",
        inline=False
    )
    embed.add_field(name="🖥️ HWID란?", value=
        "기기의 고유 식별자입니다.\n"
        "• Windows: CPU 시리얼넘버 + 마더보드 ID\n"
        "• Mac: System UUID\n"
        "• 다른 기기에서는 **절대 작동 안 함**\n"
        "• 한 번 등록되면 변경 불가능",
        inline=False
    )
    embed.add_field(name="⚠️ 주의사항", value=
        "❌ 같은 KEY를 여러 기기에서 사용 불가능\n"
        "❌ KEY를 다른 사람에게 공유하면 작동 안 함\n"
        "✅ 본인 기기에서만 사용 가능",
        inline=False
    )
    embed.set_footer(text="Gcat HUB Key System v2.0 | HWID 검증 강화")
    
    await ctx.send(embed=embed)

@bot.command(name='key_request', help='새로운 KEY 발급받기')
async def key_request(ctx, hwid: str = None):
    """새로운 KEY 발급 (HWID 검증)"""
    
    if hwid is None:
        embed = discord.Embed(
            title="❌ 오류",
            description="사용법: `!key_request <HWID>`\n\n"
            "**HWID 찾는 방법:**\n"
            "• Windows: 터미널에서 `wmic csproduct get uuid` 실행\n"
            "• Mac: 터미널에서 `system_profiler SPHardwareDataType | grep UUID` 실행",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    # HWID 형식 엄격 검증
    if not validate_hwid_format(hwid):
        embed = discord.Embed(
            title="❌ HWID 형식 오류",
            description=f"제공된 HWID: `{hwid}`\n\n"
            "**요구사항:**\n"
            "✓ 최소 16자 이상\n"
            "✓ 영문, 숫자, 하이픈(-), 언더스코어(_)만 사용\n"
            "✓ 공백 불허\n\n"
            "**올바른 예시:**\n"
            "`550E8400-E29B-41D4-A716-446655440000`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return
    
    db = load_database()
    user_id = str(ctx.author.id)
    hwid_hash = calculate_hwid_hash(hwid)
    
    # 이미 KEY가 있는지 확인
    if user_id in db["users"]:
        existing_key_info = db["users"][user_id]
        embed = discord.Embed(
            title="⚠️ 이미 KEY가 발급됨",
            description=f"당신은 이미 KEY를 받았습니다.\n\n"
            f"현재 등록된 HWID: `{existing_key_info.get('hwid_display', '표시 안됨')}`",
            color=discord.Color.orange()
        )
        embed.add_field(name="📌 확인 방법", value=
            "`!key_info` - 현재 KEY 정보 조회\n"
            "`!key_renew` - KEY 재발급 (7일 후)")
        await ctx.send(embed=embed)
        return
    
    # ⭐ 같은 HWID로 다른 사용자가 이미 등록했는지 확인
    if hwid_hash in db.get("hwid_hashes", {}):
        existing_user_id = db["hwid_hashes"][hwid_hash]
        existing_user_data = db["users"].get(existing_user_id, {})
        embed = discord.Embed(
            title="🚫 이 HWID는 이미 등록됨",
            description=f"이 기기는 이미 다른 계정에 등록되어 있습니다.\n\n"
            f"등록자: `{existing_user_data.get('user_tag', '알 수 없음')}`",
            color=discord.Color.red()
        )
        embed.add_field(name="📌 상황별 해결", value=
            "**1️⃣ 본인 계정이 맞다면:**\n"
            "기존 계정에서 `!key_info`로 KEY를 확인하세요.\n\n"
            "**2️⃣ PC를 새로 구매했다면:**\n"
            "새 HWID로 `!key_request <새HWID>`를 실행하세요.\n\n"
            "**3️⃣ 관리자 도움:**\n"
            "서버 관리자에게 문의하세요.")
        await ctx.send(embed=embed)
        logger.warning(f"[KEY] 중복 HWID 시도: {user_id} | HWID: {hwid[:20]}...")
        return
    
    # ✅ 새 KEY 생성
    new_key = generate_key()
    expiry_date = get_expiry_date()
    
    # 데이터베이스에 저장
    db["users"][user_id] = {
        "username": ctx.author.name,
        "user_tag": str(ctx.author),
        "key": new_key,
        "hwid_hash": hwid_hash,
        "hwid_display": hwid[:8] + "..." + hwid[-8:],  # 표시용 (해시는 아님)
        "created_at": datetime.now().isoformat(),
        "expires_at": expiry_date,
        "renewal_count": 0
    }
    
    # HWID 해시 등록
    db["hwid_hashes"][hwid_hash] = user_id
    
    # KEY 데이터 저장
    db["keys"][new_key] = {
        "user_id": user_id,
        "hwid_hash": hwid_hash,
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
    embed.add_field(name="🖥️ HWID", value=f"`{hwid[:8]}...{hwid[-8:]}` (해시됨)", inline=False)
    embed.add_field(name="📅 생성일", value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.add_field(name="⏰ 만료일", value=datetime.fromisoformat(expiry_date).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.add_field(name="🔒 보안", value=
        "✓ 이 기기에서만 작동\n"
        "✓ 다른 기기에서 공유 불가\n"
        "✓ 7일 후 재발급 필요", inline=False)
    embed.set_footer(text="⚠️ 이 KEY를 절대 남과 공유하지 마세요!")
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 발급] 사용자: {ctx.author} ({user_id}) | HWID: {hwid[:20]}...")

@bot.command(name='key_renew', help='KEY 재발급받기 (7일마다)')
async def key_renew(ctx):
    """KEY 재발급 (HWID 유지)"""
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
    hwid_hash = user_data["hwid_hash"]
    expiry_time = datetime.fromisoformat(user_data["expires_at"])
    current_time = datetime.now()
    
    # 재발급 가능 여부 확인 (만료 3일 전부터 가능)
    days_until_expiry = (expiry_time - current_time).days
    
    if days_until_expiry > 3:
        embed = discord.Embed(
            title="⏳ 아직 재발급할 수 없습니다",
            description=f"KEY 만료까지 **{days_until_expiry}일** 남았습니다.\n\n"
            f"만료 **3일 전부터** 재발급이 가능합니다.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return
    
    # ✅ 새 KEY 생성 (HWID는 동일하게 유지)
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
    
    db["keys"][new_key] = {
        "user_id": user_id,
        "hwid_hash": hwid_hash,
        "created_at": datetime.now().isoformat(),
        "expires_at": new_expiry
    }
    
    save_database(db)
    
    # 임베드 메시지
    embed = discord.Embed(
        title="✅ KEY 재발급 완료",
        description="새로운 KEY가 발급되었습니다.\nHWID는 유지됩니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="🔑 새 KEY", value=f"`{new_key}`", inline=False)
    embed.add_field(name="🔑 구 KEY", value=f"`{old_key}`", inline=False)
    embed.add_field(name="⏰ 새로운 만료일", value=datetime.fromisoformat(new_expiry).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    embed.add_field(name="📊 재발급 횟수", value=str(user_data["renewal_count"]), inline=False)
    embed.add_field(name="🖥️ HWID", value=f"`{user_data['hwid_display']}` (변경 안됨)", inline=False)
    embed.set_footer(text="구 KEY는 더 이상 사용할 수 없습니다.")
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 재발급] 사용자: {ctx.author} ({user_id}) | 횟수: {user_data['renewal_count']}")

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
    embed.add_field(name="🖥️ HWID", value=f"`{user_data['hwid_display']}` (해시됨)", inline=False)
    embed.add_field(name="👤 사용자", value=user_data['user_tag'], inline=False)
    embed.add_field(name="📅 발급일", value=datetime.fromisoformat(user_data['created_at']).strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="⏰ 만료일", value=expiry_time.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
    embed.add_field(name="📊 남은 일수", value=f"{max(0, days_until_expiry)}일", inline=True)
    embed.add_field(name="🔄 재발급 횟수", value=str(user_data.get('renewal_count', 0)), inline=False)
    embed.add_field(name="🔒 보안 상태", value=
        "✓ HWID 검증 활성화\n"
        "✓ 이 기기에서만 작동\n"
        "✓ 다른 기기 공유 불가", inline=False)
    
    if "last_renewed_at" in user_data:
        embed.add_field(name="🕐 마지막 재발급", value=datetime.fromisoformat(user_data['last_renewed_at']).strftime("%Y-%m-%d %H:%M:%S"), inline=False)
    
    embed.set_footer(text="⚠️ 이 정보를 누구와도 공유하지 마세요!")
    
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
    embed.add_field(name="⏰ 남은 시간", value=f"{days_remaining}일", inline=False)
    embed.add_field(name="🔒 HWID 검증", value="✓ HWID 바인딩됨 (기기별)", inline=False)
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
            value=f"KEY: `{user_data['key'][:16]}...`\n"
            f"HWID: `{user_data['hwid_display']}`\n"
            f"만료: {days_remaining}일",
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
    hwid_hash = user_data["hwid_hash"]
    
    # 데이터 삭제
    del db["users"][user_id]
    if hwid_hash in db.get("hwid_hashes", {}):
        del db["hwid_hashes"][hwid_hash]
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
    embed.add_field(name="🖥️ HWID", value=f"`{user_data['hwid_display']}`", inline=False)
    
    await ctx.send(embed=embed)
    logger.info(f"[KEY 삭제] 관리자: {ctx.author} | 대상: {user_data['user_tag']} ({user_id})")

@bot.command(name='key_admin_stats', help='KEY 시스템 통계 (관리자)')
@commands.has_role(ADMIN_ROLE)
async def key_admin_stats(ctx):
    """KEY 시스템 통계"""
    db = load_database()
    
    total_keys = len(db["users"])
    total_hwids = len(db.get("hwid_hashes", {}))
    
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
    embed.add_field(name="🔒 보안", value=
        "✓ HWID 검증 활성화\n"
        "✓ 모든 KEY는 기기에 바인딩됨\n"
        "✓ 리셀 방지 시스템 작동 중", inline=False)
    
    embed.set_footer(text="Gcat HUB Key System v2.0 | 실시간 통계")
    
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
            embed.add_field(name="🖥️ HWID", value=f"`{user_data['hwid_display']}`", inline=False)
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
    
    logger.info("=" * 60)
    logger.info("🚀 Gcat HUB KEY System v2.0 - HWID 검증 강화")
    logger.info(f"📌 Flask 포트: {PORT}")
    logger.info(f"📌 Discord 봇: 준비 중...")
    logger.info("🔒 보안: HWID 바인딩 + 기기 인증")
    logger.info("=" * 60)
    
    # Flask는 메인 스레드에서 실행
    # Discord 봇은 별도 스레드에서 실행
    bot_thread = threading.Thread(target=run_discord_bot, daemon=True)
    bot_thread.start()
    
    # Flask 서버 실행 (메인 스레드)
    run_flask()
