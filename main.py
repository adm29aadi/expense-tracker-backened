import os
import re
import json
import logging
from datetime import datetime
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import httpx
from google.oauth2.service_account import Credentials

# 1. Setup Logging Format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("expense-logger")

# FastAPI App Instance
app = FastAPI()

class ExpenseRequest(BaseModel):
    text: str

# Read secrets from Environment Variables
SECRET_API_KEY = os.getenv("SECRET_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS")

# Global variables for Google Auth
creds = None
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

@app.on_event("startup")
async def startup_event():
    global creds
    logger.info("Initializing application startup...")
    if not GOOGLE_CREDENTIALS_JSON:
        logger.error("Environment variable 'GOOGLE_CREDENTIALS' is missing!")
        return
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        logger.info("Google Service Account credentials loaded successfully.")
    except Exception as e:
        logger.error(f"Failed to load Google credentials JSON: {str(e)}")

def parse_expense(text: str):
    logger.info(f"Incoming raw text to parse: '{text}'")
    
    # Clean up the text slightly to normalize duplicate spaces
    clean_text = " ".join(text.split())
    
    # 1. Extract Amount
    amount_match = re.search(r'(?:INR|Rs\.?|Rs)\s*([\d,]+\.?\d*)', clean_text, re.IGNORECASE)
    amount = "0.00"
    if amount_match:
        amount = amount_match.group(1).replace(',', '')
        logger.info(f"Regex Match - Found Amount: {amount}")
    else:
        logger.warning("Regex Match - Could not find a valid amount pattern.")

    # 2. Extract Date
    date_match = re.search(r'(\d{2}[-\/]\d{2}[-\/]\d{2,4})', clean_text)
    date_str = datetime.now().strftime("%d-%m-%Y")
    if date_match:
        date_str = date_match.group(1)
        logger.info(f"Regex Match - Found Date: {date_str}")
    else:
        logger.info(f"Regex Match - No date found. Defaulting to current date: {date_str}")

    # 3. Extract Merchant (Smart Line Analysis for Spent & Debit SMS layouts)
    merchant = "Unknown Merchant"
    
    # Split by actual lines to find where the merchant name hides
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    potential_merchants = []
    for line in lines:
        # Ignore lines that are strictly operational bank data strings
        if any(word in line.lower() for word in ['spent', 'debited', 'debit', 'received', 'card no', 'avl limit', 'not you', 'sms block']):
            continue
        # Ignore lines that contain timestamps or time zones
        if any(word in line.upper() for word in ['ist', 'am', 'pm']) or re.search(r'\d{2}:\d{2}', line):
            continue
        # Ignore lines that contain purely phone numbers or shortcodes
        if re.match(r'^\+?[\d\s\-]{4,15}$', line):
            continue
            
        potential_merchants.append(line)
        
    if potential_merchants:
        # The first remaining clean line is highly likely our merchant (e.g., 'Sakthivel')
        merchant = potential_merchants[0]
        logger.info(f"Line Strategy - Found Merchant line: '{merchant}'")
    else:
        # Fallback to standard regex if the line splitter didn't isolate a unique line
        merchant_match = re.search(r'(?:at|to|vpa)\s+([a-zA-Z0-9\s\.\-_]+?)(?:\s+on|\s+from|\s+using|\.|\bfor\b|$)', clean_text, re.IGNORECASE)
        if merchant_match:
            merchant = merchant_match.group(1).strip()
            # Double check it's not picking up a trailing contact number from fraud blocks
            if re.search(r'\d{5,}', merchant):
                merchant = "Unknown Merchant"
            logger.info(f"Fallback Regex Match - Found Merchant: '{merchant}'")

    return amount, merchant, date_str

@app.post("/log-expense")
async def log_expense(request: ExpenseRequest, x_api_key: str = Header(None)):
    logger.info("Received a new /log-expense request.")
    
    # API Key validation check
    if x_api_key != SECRET_API_KEY:
        logger.warning(f"Unauthorized access attempt with API Key: '{x_api_key}'")
        raise HTTPException(status_code=401, detail="Unauthorized API Key")
    
    # Parse transaction string
    amount, merchant, date_str = parse_expense(request.text)
    
    # Append to Google Sheet using service account credentials
    try:
        logger.info("Requesting access token from Google OAuth...")
        import google.auth.transport.requests
        auth_request = google.auth.transport.requests.Request()
        creds.refresh(auth_request)
        access_token = creds.token
        
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A:C:append"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        # FIXED: Ordered as Date (A), Amount (B), Description/Merchant (C)
        payload = {
            "range": "Sheet1!A:C",
            "majorDimension": "ROWS",
            "values": [[date_str, amount, merchant]]
        }
        params = {
            "valueInputOption": "USER_ENTERED",
            "insertDataOption": "INSERT_ROWS"
        }
        
        logger.info(f"Sending payload to Google Sheets API row append endpoint...")
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, params=params)
            
        logger.info(f"Google Sheets API response status: {response.status_code}")
        if response.status_code != 200:
            logger.error(f"Google Sheets API Error Body: {response.text}")
            raise HTTPException(status_code=500, detail=f"Google Sheets API Error: {response.text}")
            
        logger.info("Successfully added row to the spreadsheet!")
        return {"status": "success", "data": {"date": date_str, "merchant": merchant, "amount": amount}}
        
    except Exception as e:
        logger.error(f"Execution Error during logging process: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))