import requests
from dotenv import load_dotenv
import os

# Step 1: Load credentials from the .env file
load_dotenv()
token = os.getenv("DHAN_ACCESS_TOKEN")
client_id = os.getenv("DHAN_CLIENT_ID")

# Step 2: Set up the headers Dhan API requires
headers = {
    "access-token": token,
    "client-id": client_id,
    "Content-Type": "application/json"
}

# Step 3: Call Dhan's fund limit endpoint (read-only, no trades)
print("Connecting to Dhan API...")
response = requests.get("https://api.dhan.co/fundlimit", headers=headers)

# Step 4: Show the result
if response.status_code == 200:
    data = response.json()
    print("Connection successful!")
    print(f"Available Balance : ₹{data.get('availabelBalance', 'N/A')}")
    print(f"Used Margin       : ₹{data.get('utilizedAmount', 'N/A')}")
    print(f"Total Balance     : ₹{data.get('sodLimit', 'N/A')}")
else:
    print(f"Connection failed. Status code: {response.status_code}")
    print(f"Error: {response.text}")
