import os
import time
import shutil
import base64
import uuid
import re
import requests
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2 import service_account
from PIL import Image, ImageOps
from styles_config import STYLES
from dotenv import load_dotenv

# ---------------------------------------------------------
# [설정] 구글 드라이브 폴더 ID (여기에 다시 넣어주세요!)
# ---------------------------------------------------------
ID_INBOX   = '1cYHh2k40_vyasnu__LbesA4WwAJOsSFo'
ID_DRAFT   = '1qMKhGRMXNHK98d7dIPLzandxCv6eEESt'
ID_ARCHIVE = '1MumtL0X8FslSW2r-oh7B2NMmJGsmCLeU'  # 처리된 원본 보관용

# ---------------------------------------------------------
# [설정] 구글 서비스 계정 키 파일 찾기 (로컬/Render 겸용)
# ---------------------------------------------------------
# 1. 같은 폴더에 있는지 확인 (로컬 개발용)
if os.path.exists('service_account.json'):
    SERVICE_ACCOUNT_FILE = 'service_account.json'
# 2. Render 비밀 폴더에 있는지 확인 (Render 서버용)
elif os.path.exists('/etc/secrets/service_account.json'):
    SERVICE_ACCOUNT_FILE = '/etc/secrets/service_account.json'
else:
    # 파일이 없으면 에러 발생시키지 말고 로그만 남김
    print("🚨 [비상] service_account.json 파일을 찾을 수 없습니다!")
    print("   -> Render 설정의 'Secret Files'에 파일을 등록했는지 확인하세요.")
    SERVICE_ACCOUNT_FILE = None

# AI 모델 설정
load_dotenv()
NANOBANANA_API_KEY = os.getenv("NANOBANANA_API_KEY")
MAGNIFIC_API_KEY = os.getenv("MAGNIFIC_API_KEY")
MAGNIFIC_ENDPOINT = "https://api.freepik.com/v1/ai/image-upscaler"
MODEL_NAME = 'gemini-3-pro-image-preview'

if NANOBANANA_API_KEY:
    genai.configure(api_key=NANOBANANA_API_KEY)

# 작업 임시 폴더
WORK_DIR = "temp_work"
os.makedirs(WORK_DIR, exist_ok=True)
os.makedirs("assets", exist_ok=True)

# ---------------------------------------------------------
# [기능 1] 구글 드라이브 연동 (공유 드라이브 옵션 추가됨)
# ---------------------------------------------------------
def get_drive_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/drive']
    )
    return build('drive', 'v3', credentials=creds)

def list_new_files(service, folder_id):
    # [수정] 공유 드라이브 검색 옵션 추가
    # mimeType 필터 제거 (모든 파일 감지 후 내부에서 거름)
    query = f"'{folder_id}' in parents and trashed = false"
    
    results = service.files().list(
        q=query, 
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,         # <--- 추가됨
        includeItemsFromAllDrives=True  # <--- 추가됨
    ).execute()
    
    # 이미지 파일만 필터링
    all_files = results.get('files', [])
    image_files = [f for f in all_files if 'image' in f.get('mimeType', '')]
    return image_files

def download_file(service, file_id, local_path):
    # 다운로드는 ID 기반이라 옵션 불필요하지만 안전하게 get 호출
    request = service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()

def upload_file(service, local_path, folder_id, file_name):
    print(f"   📤 업로드 중: {file_name}...", end="", flush=True)
    file_metadata = {'name': file_name, 'parents': [folder_id]}
    media = MediaFileUpload(local_path, mimetype='image/jpeg')
    
    # [수정] 공유 드라이브 업로드 옵션 추가
    service.files().create(
        body=file_metadata, 
        media_body=media, 
        fields='id',
        supportsAllDrives=True  # <--- 추가됨
    ).execute()
    print(" 완료!")

def move_file_to_archive(service, file_id, old_folder_id, new_folder_id):
    # [수정] 공유 드라이브 이동 옵션 추가
    service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=old_folder_id,
        supportsAllDrives=True  # <--- 추가됨
    ).execute()

# ---------------------------------------------------------
# [기능 2] 파일명 파싱
# ---------------------------------------------------------
def parse_filename(filename):
    name_no_ext = os.path.splitext(filename)[0]
    parts = name_no_ext.split('_')
    
    info = {
        "customer": "unknown", "room": "livingroom", 
        "style": "modern", "variant": "1", "suffix": "origin"
    }
    
    if len(parts) >= 5:
        info["customer"] = parts[0]
        info["room"] = parts[1]
        info["style"] = parts[2]
        info["variant"] = parts[3]
    elif len(parts) == 4:
        info["customer"] = parts[0]
        info["room"] = parts[1]
        info["style"] = parts[2]
        info["variant"] = "1"
    
    return info

def find_moodboard(room, style, variant):
    safe_room = room.lower().replace(" ", "")
    safe_style = style.lower().replace(" ", "-").replace("_", "-")
    target_dir = os.path.join("assets", safe_room, safe_style)
    
    if os.path.exists(target_dir):
        for f in os.listdir(target_dir):
            if f.lower().endswith(('.png', '.jpg')):
                if variant in re.findall(r'\d+', f):
                    return os.path.join(target_dir, f)
        files = [f for f in os.listdir(target_dir) if f.lower().endswith(('.png', '.jpg'))]
        if files: return os.path.join(target_dir, files[0])
    return None

# ---------------------------------------------------------
# [기능 3] AI 생성 코어
# ---------------------------------------------------------
def standardize_image(image_path):
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB': img = img.convert('RGB')
            img.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
            img.save(image_path, "JPEG", quality=95)
        return image_path
    except: return image_path

def generate_empty_room(image_path):
    print("   🔨 [1단계] 빈 방 만드는 중...", end="", flush=True)
    try:
        img = Image.open(image_path)
        prompt = (
            "IMAGE EDITING TASK (STRICT):\n"
            "Create a photorealistic image of this room but completely EMPTY.\n"
            "ACTIONS:\n"
            "1. REMOVE ALL furniture, rugs, decor, and lighting.\n"
            "2. REMOVE ALL window treatments (curtains, blinds). Show bare windows.\n"
            "3. KEEP the original floor, walls, ceiling, and windows EXACTLY as they are.\n"
            "4. IN-PAINT the removed areas seamlessly.\n"
            "OUTPUT RULE: Return ONLY the generated image."
        )
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content([prompt, img])
        
        if response.parts:
            for part in response.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    output_path = os.path.join(WORK_DIR, "temp_empty.jpg")
                    with open(output_path, 'wb') as f: f.write(part.inline_data.data)
                    print(" 완료!")
                    return standardize_image(output_path)
    except Exception as e: print(f" 실패 ({e})")
    return None

def generate_furnished(empty_path, moodboard_path):
    print(f"   🎨 [2단계] 가구 배치 중...", end="", flush=True)
    try:
        room_img = Image.open(empty_path)
        prompt = (
           "IMAGE GENERATION TASK (Virtual Staging):\n"
            "Furnish the empty room using the furniture styles shown in the Moodboard.\n\n"

            "<CRITICAL: STRUCTURAL PRESERVATION>\n"
            "1. CAMERA LOCK: Maintain the EXACT SAME camera angle, zoom, and perspective as the original image. Do NOT shift, crop, or rotate the view.\n"
            "2. GEOMETRY FREEZE: The structural lines (corners, windows, ceiling, floor) MUST remain pixel-perfectly aligned with the original.\n"
            "3. IN-PAINTING ONLY: Only remove furniture and fill in the background. Do NOT redesign the room architecture.\n\n"

            "<CRITICAL: DIMENSION & SCALE RULES>\n"
            "1. READ TEXT: You MUST read the names written on the moodboard (e.g., sofa, bed, light, chair).\n"
            "2. REALISTIC SCALING: Place the furniture in the room with accurate scale relative to the room's ceiling height (assume 2400mm ceiling).\n"
            "3. NO DISTORTION: Do not stretch or squash the furniture to fit the space. Keep the original proportions.\n\n"

            "<CRITICAL: DO NOT COPY PASTE>\n"
            "1. RE-ARRANGE: Do NOT copy the layout or composition of the moodboard. Place furniture into the room's 3D space anew.\n"
            "2. NO TEXT LABELS: IGNORE all text in the moodboard for rendering. Do NOT write any text in the final image.\n"
            "3. REMOVE BACKGROUND: Do NOT paste the white background of the moodboard. Only extract the furniture items.\n\n"

            "<LIGHTING INSTRUCTION: TURN ON ALL LIGHTS>\n"
            "1. ACTIVATE LIGHTING: Identify items labeled as 'pendant/floor/table/wall lighting'.\n"
            "2. STATE: All lighting fixtures MUST be TURNED ON.\n"
            "3. COLOR TEMPERATURE: Use 4000K White light.\n"
            "4. EMISSIVE MATERIAL: The light bulbs/shades must look bright and glowing (Emissive).\n"
            "5. AMBIENT GLOW: Ensure the lights cast a soft glow on the surrounding walls and floor.\n\n"

            "<MANDATORY WINDOW TREATMENT>\n"
            "- Install pure WHITE CHIFFON CURTAINS on all windows.\n"
            "- They must be SHEER (90% transparency), allowing natural light.\n\n"

            "<DESIGN INSTRUCTIONS>\n"
            "1. PERSPECTIVE MATCH: Align the furniture with the floor grid and vanishing points of the empty room.\n"
            "2. PLACEMENT: Realistic placement.\n"
            "OUTPUT RULE: Return ONLY the generated interior image. No text, no moodboard layout."
        )        
        input_content = [prompt, "Background Empty Room:", room_img]
        if moodboard_path:
            try:
                ref_img = Image.open(moodboard_path)
                ref_img.thumbnail((2048, 2048))
                input_content.append("Furniture Reference:")
                input_content.append(ref_img)
            except: pass
            
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(input_content)
        
        if response.parts:
            for part in response.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    unique = uuid.uuid4().hex[:6]
                    output_path = os.path.join(WORK_DIR, f"temp_furnished_{unique}.jpg")
                    with open(output_path, 'wb') as f: f.write(part.inline_data.data)
                    print(" 완료!")
                    return standardize_image(output_path)
    except Exception as e: print(f" 실패 ({e})")
    return None

def upscale_image(image_path):
    print("   ✨ [3단계] 고화질 변환 중...", end="", flush=True)
    if not MAGNIFIC_API_KEY: 
        print(" (⚠️ API키가 설정되지 않았습니다!)")
        return image_path
        
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
            
        payload = {
            "image": b64, 
            "scale_factor": "2x", 
            "optimized_for": "standard",
            "prompt": "realistic interior, highly detailed, photorealistic",
            "creativity": 1, 
            "hdr": 2, 
            "resemblance": 4, 
            "fractality": 2, 
            "engine": "automatic"
        }
        headers = {
            "x-freepik-api-key": MAGNIFIC_API_KEY, 
            "Content-Type": "application/json", 
            "Accept": "application/json"
        }
        
        res = requests.post(MAGNIFIC_ENDPOINT, json=payload, headers=headers)
        
        if res.status_code == 200:
            data = res.json()
            
            # [디버깅] 응답 데이터 확인용
            if "data" not in data:
                print(f" ⚠️ [API 응답 이상] {data}")
                return image_path

            # 1. 바로 생성된 경우
            if "generated" in data["data"] and len(data["data"]["generated"]) > 0:
                url = data["data"]["generated"][0]
                print(" 완료 (즉시생성)!")
                return download_from_url(url, image_path)
                
            # 2. 대기열(Task)에 들어간 경우
            elif "task_id" in data["data"]:
                task_id = data["data"]["task_id"]
                print(f" (대기열 {task_id})...", end="", flush=True)
                
                for _ in range(60): # 2분 대기
                    time.sleep(2)
                    check = requests.get(f"{MAGNIFIC_ENDPOINT}/{task_id}", headers=headers)
                    if check.status_code == 200:
                        s_data = check.json()
                        status = s_data.get("data", {}).get("status")
                        
                        if status == "COMPLETED":
                            if "generated" in s_data["data"] and len(s_data["data"]["generated"]) > 0:
                                url = s_data["data"]["generated"][0]
                                print(" 완료!")
                                return download_from_url(url, image_path)
                            else:
                                print(f" ⚠️ 완료됐는데 이미지가 없음: {s_data}")
                                return image_path
                                
                        elif status == "FAILED":
                            print(f" ❌ 매그니픽 작업 실패: {s_data.get('data', {}).get('message')}")
                            break
            else:
                print(f" ⚠️ 이미지 생성 실패 (데이터 없음): {data}")
                
        elif res.status_code == 401:
            print(" ❌ [인증 실패] API 키가 틀렸거나 만료되었습니다.")
        elif res.status_code == 402:
            print(" ❌ [결제 필요] 매그니픽 크레딧이 부족합니다.")
        else:
            print(f" ❌ API 에러 ({res.status_code}): {res.text}")

    except Exception as e: 
        print(f" 시스템 에러 ({e})")
        
    print(" -> (원본 화질로 저장)")
    return image_path
    
def download_from_url(url, save_path):
    res = requests.get(url)
    if res.status_code == 200:
        with open(save_path, "wb") as f: f.write(res.content)
    return save_path

# ---------------------------------------------------------
# [메인] 봇 실행 루프
# ---------------------------------------------------------
def main():
    print("🤖 AI 인테리어 봇 가동 (공유 드라이브 모드)")
    print("   Target: 1 input -> 3 variations")
    
    service = get_drive_service()
    
    while True:
        try:
            # 1. IN-BOX 확인
            files = list_new_files(service, ID_INBOX)
            
            # [디버깅 로그] (봇이 살아있는지 확인용)
            print(f"[감시 중] 발견된 파일: {len(files)}개")

            if not files:
                time.sleep(10)
                continue
            
            for file in files:
                file_id = file['id']
                file_name = file['name']
                print(f"\n📄 처리 시작: {file_name}")
                
                # 정보 파싱
                info = parse_filename(file_name)
                print(f"   ℹ️ 정보: {info['customer']} | {info['room']} | {info['style']} | {info['variant']}")
                
                # 무드보드 찾기
                ref_path = find_moodboard(info['room'], info['style'], info['variant'])
                if not ref_path:
                    print(f"   ⚠️ 무드보드를 찾을 수 없습니다. (assets 확인 필요)")
                    move_file_to_archive(service, file_id, ID_INBOX, ID_ARCHIVE)
                    continue
                
                # 다운로드
                local_path = os.path.join(WORK_DIR, file_name)
                download_file(service, file_id, local_path)
                std_path = standardize_image(local_path)
                
                # 빈 방 생성 (1회)
                empty_path = generate_empty_room(std_path)
                if not empty_path:
                    print("   ❌ 빈 방 생성 실패")
                    continue
                
                # 3장 생성 루프
                for i in range(1, 4):
                    print(f"\n   🔄 [변형 {i}/3] 생성 시작...")
                    furnished_path = generate_furnished(empty_path, ref_path)
                    
                    if furnished_path:
                        final_path = upscale_image(furnished_path)
                        output_name = f"{info['customer']}_{info['room']}_{info['style']}_{info['variant']}_render({i}).jpg"
                        upload_file(service, final_path, ID_DRAFT, output_name)
                    else:
                        print("   ❌ 생성 실패 (Skip)")
                
                # 작업 완료 후 이동
                move_file_to_archive(service, file_id, ID_INBOX, ID_ARCHIVE)
                print(f"✅ 원본 파일 이동 완료.\n")
                
                shutil.rmtree(WORK_DIR)
                os.makedirs(WORK_DIR, exist_ok=True)
                
        except Exception as e:
            print(f"\n❌ 봇 에러: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
