from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
from langdetect import detect, DetectorFactory
import datetime
from psycopg2.extras import Json
import psycopg2
import os
import json
from twilio.twiml.messaging_response import MessagingResponse
from google.cloud import dialogflow_v2 as dialogflow
from google.oauth2 import service_account
import traceback
from google.protobuf.json_format import MessageToDict



app = Flask(__name__)
DetectorFactory.seed = 0  # deterministic language detection

# -------------------
# Indian language codes
INDIAN_LANGUAGES = [
    "hi", "te", "ta", "kn", "bn", "mr", "gu", "ml", "ur", "pa", "or", "ks"
]

# -------- Slugs URL --------
SLUGS_URL = "https://raw.githubusercontent.com/INFINITE347/General_Health_stats/main/slugs.json"

def load_slugs():
    try:
        resp = requests.get(SLUGS_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error loading slugs.json: {e}")
        return {}

def get_slug(disease_param):
    slugs = load_slugs()
    key = (disease_param or "").strip().lower()
    return slugs.get(key)

# -------- Translation helpers --------
# Add your Gmail here

MYMEMORY_EMAIL = "yarramradheshreddy@gmail.com"

def translate_to_english(disease_param, detected_lang):
    """Translate incoming Indian language param to English. Skip if English."""
    if not disease_param or detected_lang == "en":
        return disease_param
    if detected_lang not in INDIAN_LANGUAGES:
        return disease_param
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={
                "q": disease_param,
                "langpair": f"{detected_lang}|en",
                "de": MYMEMORY_EMAIL  # Gmail ID to increase quota
            },
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText")
        return translated if translated else disease_param
    except Exception as e:
        print(f"Translation error (to English): {e}")
        return disease_param


def translate_from_english(text, target_lang):
    """Translate English response to Indian language if needed."""
    if not text or target_lang == "en":
        return text
    if target_lang not in INDIAN_LANGUAGES:
        return text
    try:
        resp = requests.get(
            "https://api.mymemory.translated.net/get",
            params={
                "q": text,
                "langpair": f"en|{target_lang}",
                "de": MYMEMORY_EMAIL  # Gmail ID to increase quota
            },
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        translated = data.get("responseData", {}).get("translatedText")
        return translated if translated else text
    except Exception as e:
        print(f"Translation error (from English): {e}")
        return text

# -------- Truncate helper --------
def truncate_response(text, limit=500):
    """
    Truncate text to <= limit chars, preferring to cut at the last full sentence (last '.') before limit.
    If no '.' is found, fallback to the last space and add '...'.
    """
    if not text:
        return text
    if len(text) <= limit:
        return text
    head = text[:limit]
    last_dot = head.rfind('.')
    if last_dot != -1:
        # return up to and including the last period
        return text[: last_dot + 1]
    # fallback: cut at last space and append ellipsis
    last_space = head.rfind(' ')
    if last_space != -1 and last_space > int(limit * 0.3):
        return head[:last_space] + "..."
    # as a last-resort hard cut
    return head

# -------------------
# WHO scraping helpers (all return English results)
# Each returns a heading "Intent of <DiseaseName>:\n\n" + content, truncated to 500 chars
# -------------------
def fetch_overview(url, disease_name=""):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.find(lambda tag: tag.name in ["h2","h3"] and "overview" in tag.get_text(strip=True).lower())
        if not heading:
            return None
        paragraphs = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ["h2","h3"]:
                break
            if sibling.name == "p":
                txt = sibling.get_text(strip=True)
                if txt:
                    paragraphs.append(txt)
        if not paragraphs:
            return None
        text = " ".join(paragraphs).strip()
        final_text = f"Intent of {disease_name.capitalize()}:\n\n{text}"
        return truncate_response(final_text, 500)
    except Exception:
        # log for debugging
        traceback.print_exc()
        return None

def fetch_symptoms(url, disease_name):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.find(lambda tag: tag.name in ["h2","h3"] and "symptoms" in tag.get_text(strip=True).lower())
        if not heading:
            return None
        points = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ["h2","h3"]:
                break
            if sibling.name == "ul":
                for li in sibling.find_all("li"):
                    txt = li.get_text(strip=True)
                    if txt:
                        points.append(f"🔹 {txt}")
        if not points:
            for sibling in heading.find_next_siblings():
                if sibling.name in ["h2","h3"]:
                    break
                if sibling.name == "p":
                    txt = sibling.get_text(strip=True)
                    if txt:
                        points.append(f"🔹 {txt}")
        if not points:
            return None
        body = "\n\n".join(points)
        final_text = f"Intent of {disease_name.capitalize()}:\n\n{body}"
        return truncate_response(final_text, 500)
    except Exception:
        traceback.print_exc()
        return None

def fetch_treatment(url, disease_name):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.find(lambda tag: tag.name in ["h2","h3"] and ("treatment" in tag.get_text(strip=True).lower() or "management" in tag.get_text(strip=True).lower()))
        if not heading:
            return None
        points = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ["h2","h3"]:
                break
            if sibling.name == "ul":
                for li in sibling.find_all("li"):
                    txt = li.get_text(strip=True)
                    if txt:
                        points.append(f"💊 {txt}")
        if not points:
            for sibling in heading.find_next_siblings():
                if sibling.name in ["h2","h3"]:
                    break
                if sibling.name == "p":
                    txt = sibling.get_text(strip=True)
                    if txt:
                        points.append(f"💊 {txt}")
        if not points:
            return None
        body = "\n\n".join(points)
        final_text = f"Intent of {disease_name.capitalize()}:\n\n{body}"
        return truncate_response(final_text, 500)
    except Exception:
        traceback.print_exc()
        return None

def fetch_prevention(url, disease_name):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.find(lambda tag: tag.name in ["h2","h3"] and "prevention" in tag.get_text(strip=True).lower())
        if not heading:
            return None
        points = []
        for sibling in heading.find_next_siblings():
            if sibling.name in ["h2","h3"]:
                break
            if sibling.name == "ul":
                for li in sibling.find_all("li"):
                    txt = li.get_text(strip=True)
                    if txt:
                        points.append(f"🛡️ {txt}")
        if not points:
            for sibling in heading.find_next_siblings():
                if sibling.name in ["h2","h3"]:
                    break
                if sibling.name == "p":
                    txt = sibling.get_text(strip=True)
                    if txt:
                        points.append(f"🛡️ {txt}")
        if not points:
            return None
        body = "\n\n".join(points)
        final_text = f"Intent of {disease_name.capitalize()}:\n\n{body}"
        return truncate_response(final_text, 500)
    except Exception:
        traceback.print_exc()
        return None

# -------- WHO Outbreak API --------
WHO_API_URL = (
    "https://www.who.int/api/emergencies/diseaseoutbreaknews"
    "?sf_provider=dynamicProvider372&sf_culture=en"
    "&$orderby=PublicationDateAndTime%20desc"
    "&$expand=EmergencyEvent"
    "&$select=Title,TitleSuffix,OverrideTitle,UseOverrideTitle,regionscountries,"
    "ItemDefaultUrl,FormattedDate,PublicationDateAndTime"
    "&%24format=json&%24top=10&%24count=true"
)

def get_who_outbreak_data():
    try:
        resp = requests.get(WHO_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        outbreaks = []
        for item in data.get('value', [])[:5]:
            title = item.get('OverrideTitle') or item.get('Title')
            date = item.get('FormattedDate', 'Unknown date')
            outbreaks.append(f"🦠 {title} ({date})")
        return outbreaks if outbreaks else None
    except Exception:
        traceback.print_exc()
        return None

# -------- Polio schedule --------
VACC_EMOJIS = ["💉","🕒","📅","⚠️","ℹ️","🎯","👶","🏥","⚕️","✅","⏰","📢"]

def build_polio_schedule(birth_date):
    schedule = [
        ("At Birth (within 15 days)", birth_date, "OPV-0"),
        ("6 Weeks", birth_date + datetime.timedelta(weeks=6), "OPV-1 + IPV-1"),
        ("10 Weeks", birth_date + datetime.timedelta(weeks=10), "OPV-2"),
        ("14 Weeks", birth_date + datetime.timedelta(weeks=14), "OPV-3 + IPV-2"),
        ("16–24 Months", birth_date + datetime.timedelta(weeks=72), "OPV + IPV Boosters"),
        ("5 Years", birth_date + datetime.timedelta(weeks=260), "OPV Booster")
    ]
    return schedule

# -------- PostgreSQL / Memory --------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    conn = None
    _in_memory_store = {}
else:
    try:
        conn = psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"DB connection error: {e}")
        conn = None
        _in_memory_store = {}

def create_users_table():
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                context JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"Users table error: {e}")

create_users_table()

def get_user_memory(user_id):
    if not user_id:
        return {}
    if not conn:
        return _in_memory_store.get(user_id, {}).copy()
    try:
        cur = conn.cursor()
        cur.execute("SELECT context FROM users WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else {}
    except Exception as e:
        print(f"DB get_user_memory error: {e}")
        return {}

def save_user_memory(user_id, context):
    if not user_id:
        return
    if not conn:
        _in_memory_store[user_id] = context.copy()
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (user_id, context, last_updated)
            VALUES (%s, %s, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET context = EXCLUDED.context, last_updated = NOW()
        """, (user_id, Json(context)))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"DB save_user_memory error: {e}")

# -------- Flask Webhook --------
@app.route('/webhook', methods=['POST'])
def webhook():
    req = request.get_json(silent=True)
    if not req:
        return jsonify({"fulfillmentText": "Invalid request"}), 400

    user_id = req.get("originalDetectIntentRequest", {}).get("payload", {}).get("user", {}).get("userId") or req.get("session")
    intent_name = req.get("queryResult", {}).get("intent", {}).get("displayName", "")
    params = req.get("queryResult", {}).get("parameters", {}) or {}
    date_str = params.get("date", "")
    disease_input = (params.get("disease", "") or params.get("any", "") or "").strip()

    memory = get_user_memory(user_id) or {}
    memory.setdefault("last_disease", "")
    memory.setdefault("user_lang", "en")
    memory.setdefault("last_queries", [])

    # Detect language
    try:
        detected_lang = detect(disease_input) if disease_input else memory.get("user_lang", "en")
    except Exception:
        detected_lang = memory.get("user_lang", "en")

    # If user provided param -> translate only if Indian language
    if disease_input:
        disease_param = translate_to_english(disease_input, detected_lang) or disease_input
        disease_param = disease_param.strip().lower()
        user_lang = detected_lang if detected_lang in INDIAN_LANGUAGES else "en"
    else:
        disease_param = memory.get("last_disease", "")
        user_lang = memory.get("user_lang", "en")

    memory["last_disease"] = disease_param
    memory["user_lang"] = user_lang

    now_iso = datetime.datetime.utcnow().isoformat()
    memory.setdefault("last_queries", [])
    memory["last_queries"].append({
        "intent": intent_name,
        "disease": disease_param,
        "user_lang": user_lang,
        "timestamp": now_iso
    })
    memory["last_queries"] = memory["last_queries"][-5:]

    response_text = "Sorry, I don't understand your request."

    try:
        if intent_name == "get_disease_overview":
            response_text = f"📖 DISEASE OVERVIEW OF {disease_param}\n\n"
            if not disease_param:
                response_text += "No disease provided."
            else:
                slug = get_slug(disease_param)
                if slug:
                    url = f"https://www.who.int/news-room/fact-sheets/detail/{slug}"
                    section = fetch_overview(url, disease_param)
                    response_text += section or f"Overview not found for {disease_param}."
                else:
                    response_text += f"Disease not found: {disease_param}."

        elif intent_name == "get_symptoms":
            response_text = f"🤒 SYMPTOMS OF {disease_param}\n\n"
            if not disease_param:
                response_text += "No disease provided."
            else:
                slug = get_slug(disease_param)
                if slug:
                    url = f"https://www.who.int/news-room/fact-sheets/detail/{slug}"
                    section = fetch_symptoms(url, disease_param)
                    response_text += section or f"Symptoms not found for {disease_param}."
                else:
                    response_text += f"No URL found for {disease_param}."

        elif intent_name == "get_treatment":
            response_text = f"💊 TREATMENT OF {disease_param}\n\n"
            if not disease_param:
                response_text += "No disease provided."
            else:
                slug = get_slug(disease_param)
                if slug:
                    url = f"https://www.who.int/news-room/fact-sheets/detail/{slug}"
                    section = fetch_treatment(url, disease_param)
                    response_text += section or f"Treatment not found for {disease_param}."
                else:
                    response_text += f"No URL found for {disease_param}."

        elif intent_name == "get_prevention":
            response_text = f"🛡️ PREVENTION OF {disease_param}\n\n"
            if not disease_param:
                response_text += "No disease provided."
            else:
                slug = get_slug(disease_param)
                if slug:
                    url = f"https://www.who.int/news-room/fact-sheets/detail/{slug}"
                    section = fetch_prevention(url, disease_param)
                    response_text += section or f"Prevention not found for {disease_param}."
                else:
                    response_text += f"No URL found for {disease_param}."

        elif intent_name == "disease_outbreak.general":
            response_text = "🌍 LATEST OUTBREAK NEWS\n\n"
            outbreaks = get_who_outbreak_data()
            response_text += '\n\n'.join(outbreaks) if outbreaks else "Unable to fetch outbreak data."

        elif intent_name == "get_vaccine":
            response_text = "💉 POLIO VACCINATION SCHEDULE\n\n"
            birth_date = datetime.date.today()
            if date_str:
                try:
                    birth_date = datetime.datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").date()
                except Exception:
                    pass

            schedule = build_polio_schedule(birth_date)
            for idx, (period, date, vaccine) in enumerate(schedule):
                emoji = VACC_EMOJIS[idx]
                response_text += f"{emoji} {period}: {date.strftime('%d-%b-%Y')} → {vaccine}\n"

            # --- Extra Information Block ---
            extra_info = [
                ("⚠️", "Disease & Symptoms: Polio causes fever,weakness,headache,vomiting,stiffness,paralysis"),
                ("ℹ️", "About the Vaccine: OPV (oral drops),IPV (injection)"),
                ("⚕️", "Side Effects: Safe; rarely mild fever."),
            ]

            response_text += "\n\n📘 ADDITIONAL INFORMATION\n"
            for emoji, text in extra_info:
                response_text += f"{emoji} {text}\n\n"

        elif intent_name == "get_last_queries":
            saved = memory.get("last_queries", [])
            if not saved:
                response_text = "No past queries stored."
            else:
                lines = [f"{q.get('timestamp','')} · {q.get('intent','')} · {q.get('disease','')}" for q in saved]
                response_text = "Your last queries:\n" + '\n'.join(lines)

        elif intent_name == "Default Fallback Intent":
            response_text = " "

        # Translate final response if user_lang != en
        response_text = translate_from_english(response_text, user_lang)

    except Exception:
        traceback.print_exc()
        response_text = "⚠️ An error occurred while processing your request."

    save_user_memory(user_id, memory)
    return jsonify({"fulfillmentText": response_text})
# ------------------

# ----------------------
# Dialogflow setup
# ----------------------
# ------------------- Dialogflow Setup -------------------
PROJECT_ID = os.getenv("DIALOGFLOW_PROJECT_ID")  # Your Dialogflow Project ID
google_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")  # JSON Key as string

if not PROJECT_ID or not google_creds_json:
    raise ValueError("❌ Missing DIALOGFLOW_PROJECT_ID or GOOGLE_CREDENTIALS_JSON env variables")

credentials_info = json.loads(google_creds_json)
GOOGLE_CREDENTIALS = service_account.Credentials.from_service_account_info(credentials_info)
def detect_intent_text(session_id, text, language_code="en"):
    """
    Send text to Dialogflow and get the fulfillment response.
    Always returns dict with fulfillment_text, intent, and parameters.
    """
    try:
        session_client = dialogflow.SessionsClient(credentials=GOOGLE_CREDENTIALS)
        session = session_client.session_path(PROJECT_ID, session_id)

        text_input = dialogflow.TextInput(text=text, language_code=language_code)
        query_input = dialogflow.QueryInput(text=text_input)

        response = session_client.detect_intent(session=session, query_input=query_input)

        # Extract safely
        fulfillment_text = response.query_result.fulfillment_text or " "
        intent_name = response.query_result.intent.display_name if response.query_result.intent else ""
        # Make sure parameters is always a dict
        try:
            parameters = dict(response.query_result.parameters) if response.query_result.parameters else {}
        except Exception:
            parameters = {}

        return {
            "fulfillment_text": fulfillment_text,
            "intent": intent_name,
            "parameters": parameters
        }

    except Exception:
        traceback.print_exc()
        return {
            "fulfillment_text": "⚠️ Something went wrong while connecting to Dialogflow.",
            "intent": "",
            "parameters": {}
        }



# ------------------- WhatsApp Webhook -------------------

# ------------------- WhatsApp Webhook -------------------
@app.route("/whatsapp_webhook", methods=["POST"])
def whatsapp_webhook():
    try:
        # ------------------- Incoming Message -------------------
        incoming_msg = request.form.get("Body")
        from_number = request.form.get("From")
        session_id = from_number or "default_user"

        # ------------------- Persistent Memory -------------------
        memory = get_user_memory(session_id) or {}
        memory.setdefault("user_lang", "en")
        memory.setdefault("last_disease", "")
        memory.setdefault("last_queries", [])

        # Detect user language
        try:
            detected_lang = detect(incoming_msg) if incoming_msg else memory.get("user_lang", "en")
        except Exception:
            detected_lang = memory.get("user_lang", "en")
        user_lang = detected_lang if detected_lang in INDIAN_LANGUAGES else "en"

        # Translate message to English for Dialogflow
        english_text = translate_to_english(incoming_msg, detected_lang)

        # ------------------- Dialogflow Intent -------------------
        df_result = detect_intent_text(session_id, english_text)

        fulfillment_text = df_result.get("fulfillment_text", "")
        parameters = df_result.get("parameters", {}) or {}
        intent_name = df_result.get("intent", "")
        disease_param = parameters.get("disease") or memory.get("last_disease", "")

        # Update persistent memory
        memory["last_disease"] = disease_param
        memory["user_lang"] = user_lang
        memory["last_queries"].append({
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "intent": intent_name,
            "disease": disease_param
        })
        memory["last_queries"] = memory["last_queries"][-5:]
        save_user_memory(session_id, memory)

        # ------------------- Build Response -------------------
        response_text = fulfillment_text or " "

        # Disease-related intents
        if disease_param:
            slug = get_slug(disease_param.lower())
            if slug:
                url = f"https://www.who.int/news-room/fact-sheets/detail/{slug}"
                if intent_name == "get_disease_overview":
                    section = fetch_overview(url, disease_param)
                    response_text += f"\n\n{section}" if section else f"\n\nOverview not found for {disease_param}."
                elif intent_name == "get_symptoms":
                    section = fetch_symptoms(url, disease_param)
                    response_text += f"\n\n{section}" if section else f"\n\nSymptoms not found for {disease_param}."
                elif intent_name == "get_treatment":
                    section = fetch_treatment(url, disease_param)
                    response_text += f"\n\n{section}" if section else f"\n\nTreatment not found for {disease_param}."
                elif intent_name == "get_prevention":
                    section = fetch_prevention(url, disease_param)
                    response_text += f"\n\n{section}" if section else f"\n\nPrevention not found for {disease_param}."

        # Outbreak intent
        if intent_name == "disease_outbreak.general":
            outbreaks = get_who_outbreak_data()
            response_text += "\n\n" + "\n".join(outbreaks) if outbreaks else "\n\nUnable to fetch outbreak data."

        # Vaccination intent
        if intent_name == "get_vaccine":
            birth_date = datetime.date.today()
            schedule = build_polio_schedule(birth_date)
            response_text += "\n\n💉 Polio Vaccination Schedule\n"
            for idx, (period, date, vaccine) in enumerate(schedule):
                emoji = VACC_EMOJIS[idx % len(VACC_EMOJIS)]
                response_text += f"{emoji} {period}: {date.strftime('%d-%b-%Y')} → {vaccine}\n"

        # Translate back to user language
        response_text = translate_from_english(response_text, user_lang)

        # ------------------- Send via Twilio -------------------
        twilio_resp = MessagingResponse()
        twilio_resp.message(response_text)
        return str(twilio_resp)

    except Exception:
        traceback.print_exc()
        twilio_resp = MessagingResponse()
        twilio_resp.message("⚠️ Something went wrong. Please try again later.")
        return str(twilio_resp)




# ----------------------
# Run Flask app
# ----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
