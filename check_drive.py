import os
from google.oauth2 import service_account
from googleapiclient.discovery import build

# -----------------------------------------------------
# [진단 대상] 확인하고 싶은 폴더 ID (01_INBOX)
TARGET_FOLDER_ID = '1cYHh2k40_vyasnu__LbesA4WwAJOsSFo'
# -----------------------------------------------------

SERVICE_ACCOUNT_FILE = 'service_account.json'

def run_diagnosis():
    print("----- 🕵️‍♂️ 구글 드라이브 진단 (공유 드라이브 모드) -----")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print("❌ [오류] service_account.json 파일이 없습니다.")
        return

    try:
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds)
        print(f"✅ [로그인] 봇 이메일: {creds.service_account_email}")

        # ---------------------------------------------------------
        # [수정된 부분] supportsAllDrives=True 옵션 추가
        # ---------------------------------------------------------
        print(f"🔍 폴더 ID ({TARGET_FOLDER_ID}) 접속 시도...")
        
        try:
            # 폴더 정보 가져오기 (공유 드라이브 지원 옵션 추가)
            folder = service.files().get(
                fileId=TARGET_FOLDER_ID,
                supportsAllDrives=True  # <--- 이게 핵심입니다!
            ).execute()
            print(f"✅ [접속 성공] 폴더 이름: '{folder['name']}'")
        except Exception as e:
            print(f"❌ [접속 실패] 폴더를 못 찾았습니다. ID를 다시 확인해주세요.")
            print(f"   에러: {e}")
            return

        print("🔍 파일 스캔 중...")
        
        # 파일 목록 가져오기 (공유 드라이브 지원 옵션 추가)
        query = f"'{TARGET_FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,         # <--- 필수
            includeItemsFromAllDrives=True  # <--- 필수
        ).execute()
        
        files = results.get('files', [])

        print(f"📊 발견된 파일: {len(files)}개")
        
        if files:
            for f in files:
                print(f"   - {f['name']}")
        else:
            print("⚠️ 폴더가 비어 있습니다 (혹은 권한 문제).")

    except Exception as e:
        print(f"❌ 시스템 에러: {e}")

if __name__ == "__main__":
    run_diagnosis()