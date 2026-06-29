import os
import re
import json
from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import httpx
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

app = FastAPI()

# Security: API Key to protect your endpoint
API_KEY = os.environ.get("SECRET_API_KEY", "Adarshdev2000")
api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=True)

# HARDCODE SPREADSHEET ID BYPASS 
# Replace the string below with your exact Spreadsheet ID from the URL bar
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1hDTNNch83WI5KiHbqJbysvaJ6u1eWh5ERrhOp_2iKOg") 

class SMSPayload(BaseModel):
    text: str

def get_access_token():
    """Gets a clean, direct OAuth2 access token from Google Credentials."""
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    
    env_creds = os.environ.get("GOOGLE_CREDENTIALS")
    if env_creds:
        creds_dict = json.loads(env_creds)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    else:
        creds = Credentials.from_service_account_file("creds.json", scopes=scope)
        
    creds.refresh(Request())
    return creds.token

def parse_axis_bank_sms(text: str):
    """Parses Axis Bank notifications."""
    try:
        cleaned_text = " ".join(text.split())
        
        amount_match = re.search(r"(?:Spent|debited with)\s+INR\s*([\d\.]+)", cleaned_text, re.IGNORECASE)
        date_match = re.search(r"on\s*([\d-]+)", cleaned_text, re.IGNORECASE)
        by_match = re.search(r"(?:by|at)\s+([A-Za-z0-9\/_\.\-\s]+)", cleaned_text, re.IGNORECASE)

        amount = float(amount_match.group(1)) if amount_match else 0.0
        date = date_match.group(1) if date_match else ""
        
        description = "Axis Bank Transaction"
        if by_match:
            desc_candidate = by_match.group(1).strip()
            description = desc_candidate.split(".")[0] if "." in desc_candidate else desc_candidate

        return date, amount, description
    except Exception as e:
        raise ValueError(f"Parsing error: {str(e)}")

@app.post("/log-expense")
async def log_expense(payload: SMSPayload, api_key: str = Security(api_key_header)):
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    
    try:
        date, amount, description = parse_axis_bank_sms(payload.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if amount == 0.0:
        raise HTTPException(status_code=400, detail="Could not extract a valid transaction amount.")

    try:
        # Get clean access token
        token = get_access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        async with httpx.AsyncClient() as client:
            # Append rows natively via Google Sheets API v4 using the direct ID
            append_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A:C:append?valueInputOption=USER_ENTERED"
            body = {
                "range": "Sheet1!A:C",
                "majorDimension": "ROWS",
                "values": [[date, amount, description]]
            }
            
            response = await client.post(append_url, headers=headers, json=body)
            
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=f"Google API Error: {response.text}")
                
        return {
            "status": "success", 
            "logged": {"date": date, "amount": amount, "description": description}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google Integration Error: {str(e)}")