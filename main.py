"""
더스틴 챗봇 답변 품질 자동 검증 시스템
- 구글시트에서 질문 읽기 → 더스틴 API 호출 → Gemini로 채점 → 결과 저장
- 비용: 전부 무료 (Gemini API Free Tier + GitHub Actions)
"""

import os
import json
import time
import uuid
import re
from datetime import datetime
from urllib.parse import unquote

import gspread
from google.oauth2.service_account import Credentials
import requests
import google.generativeai as genai


# ============================================================
# 설정
# ============================================================
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
CHATBOT_BASE_URL = "https://ai-chatbot.lotteshopping.com"
BRANCH_CODE = os.environ.get("BRANCH_CODE", "0002")  # 기본값: 잠실점

# Gemini API 설정 (무료)
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

# 구글시트 연결
creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
creds = Credentials.from_service_account_info(
    creds_json,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
gc = gspread.authorize(creds)
spreadsheet = gc.open_by_key(SPREADSHEET_ID)


# ============================================================
# 1. 구글시트에서 질문 목록 읽기
# ============================================================
def get_questions():
    """'질문목록' 시트에서 질문 데이터를 읽어옵니다."""
    sheet = spreadsheet.worksheet("질문목록")
    rows = sheet.get_all_records()
    questions = []
    for row in rows:
        q = str(row.get("질문", "")).strip()
        if q:
            questions.append({
                "category": str(row.get("카테고리", "")),
                "question": q,
                "expected_keywords": str(row.get("기대답변_키워드", "")),
            })
    return questions


# ============================================================
# 2. 더스틴 챗봇 API 호출
# ============================================================
def get_auth_token():
    try:
        resp = requests.post(
            f"{CHATBOT_BASE_URL}/auth/token",
            headers={
                "Content-Type": "application/json",
                "Origin": "https://m.lotteshopping.com",
                "Referer": "https://m.lotteshopping.com/chatbot/aiChatbot?cstrCd=0002",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            },
            json={},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # 토큰 필드명 자동 탐색
        token = (
            data.get("token")
            or data.get("access_token")
            or data.get("accessToken")
            or data.get("Authorization")
            or ""
        )
        return token
    except Exception as e:
        print(f"  ⚠️ 토큰 발급 실패: {e}")
        return ""


def generate_session_id():
    """고유 세션 ID를 생성합니다."""
    short_id = uuid.uuid4().hex[:16]
    return f"LD_CHAT_qa_{short_id}"


def parse_sse_response(response):
    """
    SSE(Server-Sent Events) 스트리밍 응답을 파싱합니다.
    각 'data: ...' 라인을 URL 디코딩하여 텍스트를 조합합니다.
    """
    full_text = ""
    metadata = {}
    follow_up = {}

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue

        chunk = line[6:]  # "data: " 제거

        # 스트림 종료
        if chunk == "[DONE]":
            break

        # URL 디코딩
        decoded = unquote(chunk)

        # JSON 메타데이터 블록 감지 (매장 정보, 후속 질문 등)
        if decoded.startswith("{") and decoded.endswith("}"):
            try:
                parsed = json.loads(decoded)
                if "answerType" in parsed:
                    metadata = parsed
                elif "follow_up_questions" in parsed:
                    follow_up = parsed
                continue
            except json.JSONDecodeError:
                pass

        full_text += decoded

    # HTML 태그 제거하여 순수 텍스트 추출
    clean_text = re.sub(r"<br\s*/?>", "\n", full_text)
    clean_text = re.sub(r"<[^>]+>", "", clean_text)
    clean_text = clean_text.strip()

    return {
        "text": clean_text,
        "metadata": metadata,
        "follow_up": follow_up.get("follow_up_questions", []),
    }


def ask_chatbot(question_text, session_id, auth_token=""):
    """더스틴 챗봇 API에 질문을 보내고 답변을 받습니다."""

headers = {
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "Origin": "https://m.lotteshopping.com",
    "Referer": "https://m.lotteshopping.com/chatbot/aiChatbot?cstrCd=0002",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    payload = {
        "stream": True,
        "user_id": "",
        "session_id": session_id,
        "answer_type": "",
        "branch_code": "",
        "current_branch": BRANCH_CODE,
        "shop_nm": "",
        "user_message_content": question_text,
    }

    start_time = time.time()

    try:
        response = requests.post(
            f"{CHATBOT_BASE_URL}/api/chat",
            headers=headers,
            json=payload,
            timeout=60,
            stream=True,  # SSE 스트리밍 수신
        )
        response.raise_for_status()
        elapsed = round(time.time() - start_time, 2)

        # SSE 응답 파싱
        result = parse_sse_response(response)

        return {
            "answer": result["text"],
            "metadata": result["metadata"],
            "follow_up": result["follow_up"],
            "elapsed": elapsed,
            "error": None,
        }

    except Exception as e:
        elapsed = round(time.time() - start_time, 2)
        return {
            "answer": "",
            "metadata": {},
            "follow_up": [],
            "elapsed": elapsed,
            "error": str(e),
        }


# ============================================================
# 3. Gemini API로 품질 채점 (무료)
# ============================================================
def evaluate_answer(question, expected_keywords, chatbot_answer):
    """Gemini API를 사용해 챗봇 답변의 품질을 1~5점으로 채점합니다."""
    if not chatbot_answer:
        return {"score": 0, "comment": "답변 없음 (API 오류 또는 빈 응답)"}

    eval_prompt = f"""당신은 백화점 AI 챗봇(더스틴)의 답변 품질을 평가하는 전문가입니다.
아래 챗봇 답변을 평가해주세요.

[고객 질문]
{question}

[기대 답변에 포함되어야 할 키워드]
{expected_keywords}

[챗봇 실제 답변]
{chatbot_answer}

아래 기준으로 1~5점 채점하세요:
- 5점: 정확하고 친절하며 키워드 모두 포함, 고객이 만족할 수준
- 4점: 대체로 정확하나 일부 누락 또는 표현 개선 필요
- 3점: 부분적으로 맞지만 핵심 정보가 빠짐
- 2점: 관련은 있으나 부정확하거나 불충분한 답변
- 1점: 완전히 틀리거나 "답변드리기 어렵습니다" 등 답변 거부

반드시 아래 JSON 형식으로만 응답하세요. 다른 텍스트는 포함하지 마세요:
{{"score": 점수, "comment": "평가 이유 (한국어, 1~2문장)"}}"""

    try:
        response = gemini_model.generate_content(eval_prompt)
        result_text = response.text.strip()
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(result_text)
        return {"score": int(result["score"]), "comment": str(result["comment"])}
    except Exception as e:
        return {"score": -1, "comment": f"채점 오류: {str(e)}"}


# ============================================================
# 4. 기대 키워드 포함 여부 체크
# ============================================================
def check_keywords(answer, expected_keywords_str):
    """기대 키워드가 답변에 포함되어 있는지 확인합니다."""
    if not answer or not expected_keywords_str:
        return "확인불가"
    keywords = [k.strip() for k in expected_keywords_str.split(",") if k.strip()]
    if not keywords:
        return "키워드 없음"
    missing = [k for k in keywords if k not in answer]
    if not missing:
        return "✅ 모두 포함"
    return f"⚠️ 누락: {', '.join(missing)}"


# ============================================================
# 5. 결과를 구글시트에 기록
# ============================================================
def save_results(results):
    """검증 결과를 '검증결과' 시트에 저장합니다."""
    header = [
        "날짜", "점포", "카테고리", "질문", "챗봇답변",
        "AI점수(1~5)", "AI코멘트", "기대키워드포함",
        "응답시간(초)", "에러",
    ]

    try:
        sheet = spreadsheet.worksheet("검증결과")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="검증결과", rows=1000, cols=12)
        sheet.append_row(header)
        sheet.format("1:1", {"textFormat": {"bold": True}})

    branch_names = {
        "0002": "잠실점", "0001": "본점", "0007": "부산본점",
        "0009": "대구점", "0015": "광주점",
    }
    branch_name = branch_names.get(BRANCH_CODE, BRANCH_CODE)

    rows_to_add = []
    for r in results:
        rows_to_add.append([
            r["date"],
            branch_name,
            r["category"],
            r["question"],
            r["answer"][:1000],
            r["score"],
            r["comment"],
            r["keyword_check"],
            r["elapsed"],
            r["error"] or "",
        ])

    if rows_to_add:
        sheet.append_rows(rows_to_add)
    print(f"  ✅ {len(rows_to_add)}건 결과 저장 완료")


# ============================================================
# 6. 메인 실행
# ============================================================
def main():
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"🚀 더스틴 챗봇 품질 검증 시작: {today}")
    print(f"📍 점포 코드: {BRANCH_CODE}")
    print("=" * 50)

    # 인증 토큰 발급
    print("🔑 인증 토큰 발급 중...")
    auth_token = get_auth_token()
    if auth_token:
        print("  ✅ 토큰 발급 완료")
    else:
        print("  ⚠️ 토큰 없이 진행합니다")

    # 질문 로드
    questions = get_questions()
    print(f"📋 질문 {len(questions)}개 로드 완료\n")

    results = []
    error_count = 0
    low_score_count = 0

    for i, q in enumerate(questions):
        print(f"[{i+1}/{len(questions)}] {q['category']} | {q['question']}")

        # 각 질문마다 새 세션 (대화 맥락 간섭 방지)
        q_session = generate_session_id()

        # 챗봇에 질문
        chatbot_result = ask_chatbot(q["question"], q_session, auth_token)
        if chatbot_result["error"]:
            print(f"  ❌ 챗봇 오류: {chatbot_result['error']}")
            error_count += 1
        else:
            preview = chatbot_result["answer"][:80].replace("\n", " ")
            print(f"  📨 응답 ({chatbot_result['elapsed']}초): {preview}...")

        # Gemini로 품질 채점
        eval_result = evaluate_answer(
            q["question"],
            q["expected_keywords"],
            chatbot_result["answer"],
        )
        score = eval_result["score"]
        if 0 < score <= 2:
            low_score_count += 1
        print(f"  🏆 점수: {score}/5 — {eval_result['comment']}")

        # 키워드 포함 여부
        keyword_check = check_keywords(
            chatbot_result["answer"],
            q["expected_keywords"],
        )
        print(f"  🔑 키워드: {keyword_check}\n")

        results.append({
            "date": today,
            "category": q["category"],
            "question": q["question"],
            "answer": chatbot_result["answer"],
            "score": score,
            "comment": eval_result["comment"],
            "keyword_check": keyword_check,
            "elapsed": chatbot_result["elapsed"],
            "error": chatbot_result["error"],
        })

        # API 호출 간격 (Gemini 무료 티어 10 RPM 보호)
        time.sleep(15)

    # 결과 저장
    print("=" * 50)
    save_results(results)

    # 요약
    scores = [r["score"] for r in results if r["score"] > 0]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0

    print(f"\n📊 검증 요약")
    print(f"  총 {len(results)}건 테스트")
    print(f"  평균 점수: {avg_score}/5")
    print(f"  저품질(2점 이하): {low_score_count}건")
    print(f"  오류: {error_count}건")

    if low_score_count > 0:
        print(f"\n  ⚠️ 저품질 답변 {low_score_count}건 — 구글시트에서 확인하세요!")

    # (선택) 슬랙 알림
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url and low_score_count > 0:
        try:
            requests.post(slack_url, json={
                "text": (
                    f"⚠️ 더스틴 챗봇 품질 알림\n"
                    f"날짜: {today} | 점포: {BRANCH_CODE}\n"
                    f"총 {len(results)}건 중 저품질 {low_score_count}건\n"
                    f"평균 점수: {avg_score}/5"
                )
            })
            print("  📢 슬랙 알림 전송 완료")
        except Exception as e:
            print(f"  슬랙 알림 실패: {e}")


if __name__ == "__main__":
    main()
