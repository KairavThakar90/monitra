import requests

login_url = "https://monitra-lvzq.vercel.app/api/v1/auth/login"
login_payload = {
    "username": "bharat@storetransform.com",
    "password": "Password123!"
}

response = requests.post(login_url, json=login_payload)
print(f"Login Status: {response.status_code}")
print(f"Login Response: {response.text}")
token = response.json().get("access_token")

if token:
    url = "https://monitra-lvzq.vercel.app/api/v1/members/239"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "capture_frequency": 15,
        "idle_enabled": False,
        "idle_minutes": 10
    }
    r = requests.patch(url, headers=headers, json=payload)
    print(f"Patch Status: {r.status_code}")
    print(f"Patch Response: {r.text}")
