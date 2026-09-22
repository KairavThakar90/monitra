import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import get_session_local
from app.models.screenshot_application import ScreenshotApplication
from app.models.screenshot_url import ScreenshotUrl

APPLICATIONS = [
    ("Google Chrome", "chrome.exe", "Browsers"),
    ("Microsoft Edge", "msedge.exe", "Browsers"),
    ("Mozilla Firefox", "firefox.exe", "Browsers"),
    ("Opera", "opera.exe", "Browsers"),
    ("Brave Browser", "brave.exe", "Browsers"),
    ("Safari", "safari", "Browsers"),
    ("Microsoft Teams", "ms-teams.exe", "Communication"),
    ("Slack", "slack.exe", "Communication"),
    ("Zoom", "zoom.exe", "Communication"),
    ("Google Meet", "chrome.exe", "Communication"), # Web app mostly but handled via URL usually
    ("Cisco Webex", "webex.exe", "Communication"),
    ("Microsoft Outlook", "outlook.exe", "Email"),
    ("Microsoft Word", "winword.exe", "Office & Productivity"),
    ("Microsoft Excel", "excel.exe", "Office & Productivity"),
    ("Microsoft PowerPoint", "powerpnt.exe", "Office & Productivity"),
    ("Microsoft OneNote", "onenote.exe", "Office & Productivity"),
    ("Microsoft OneDrive", "onedrive.exe", "Office & Productivity"),
    ("SharePoint", "sharepoint", "Office & Productivity"),
    ("Google Drive", "googledrive.exe", "Office & Productivity"),
    ("Google Docs", "chrome.exe", "Office & Productivity"),
    ("Google Sheets", "chrome.exe", "Office & Productivity"),
    ("Google Slides", "chrome.exe", "Office & Productivity"),
    ("Notion", "notion.exe", "Office & Productivity"),
    ("Trello", "trello.exe", "Project Management"),
    ("Asana", "asana.exe", "Project Management"),
    ("Monday.com", "monday.exe", "Project Management"),
    ("ClickUp", "clickup.exe", "Project Management"),
    ("Jira", "jira.exe", "Project Management"),
    ("Confluence", "confluence.exe", "Project Management"),
    ("GitHub Desktop", "githubdesktop.exe", "Development"),
    ("GitHub", "chrome.exe", "Development"),
    ("GitLab", "chrome.exe", "Development"),
    ("Bitbucket", "chrome.exe", "Development"),
    ("Visual Studio Code", "code.exe", "Development"),
    ("Visual Studio", "devenv.exe", "Development"),
    ("JetBrains IntelliJ IDEA", "idea64.exe", "Development"),
    ("JetBrains PyCharm", "pycharm64.exe", "Development"),
    ("JetBrains WebStorm", "webstorm64.exe", "Development"),
    ("Android Studio", "studio64.exe", "Development"),
    ("Postman", "postman.exe", "Development"),
    ("Insomnia", "insomnia.exe", "Development"),
    ("Docker Desktop", "docker.exe", "Development"),
    ("Figma", "figma.exe", "Design"),
    ("Adobe Photoshop", "photoshop.exe", "Design"),
    ("Adobe Illustrator", "illustrator.exe", "Design"),
    ("Adobe XD", "xd.exe", "Design"),
    ("Canva", "canva.exe", "Design"),
    ("Dropbox", "dropbox.exe", "Cloud Storage"),
    ("1Password", "1password.exe", "Security"),
    ("Bitwarden", "bitwarden.exe", "Security"),
    ("LastPass", "lastpass.exe", "Security"),
    ("WhatsApp", "whatsapp.exe", "Messaging"),
    ("Telegram", "telegram.exe", "Messaging"),
    ("Discord", "discord.exe", "Messaging"),
    ("Skype", "skype.exe", "Messaging"),
    ("VLC Media Player", "vlc.exe", "Media"),
    ("Spotify", "spotify.exe", "Media"),
    ("Windows Terminal", "wt.exe", "System Utilities"),
    ("PowerShell", "powershell.exe", "System Utilities"),
    ("Command Prompt", "cmd.exe", "System Utilities"),
    ("File Explorer", "explorer.exe", "System Utilities"),
    ("Remote Desktop", "mstsc.exe", "Remote Access"),
    ("AnyDesk", "anydesk.exe", "Remote Access"),
    ("TeamViewer", "teamviewer.exe", "Remote Access"),
]

URLS = [
    ("GitHub", "github.com", "https://github.com/*", "Development & IT"),
    ("GitLab", "gitlab.com", "https://gitlab.com/*", "Development & IT"),
    ("Bitbucket", "bitbucket.org", "https://bitbucket.org/*", "Development & IT"),
    ("Google Drive", "drive.google.com", "https://drive.google.com/*", "Cloud Storage"),
    ("Gmail", "mail.google.com", "https://mail.google.com/*", "Email"),
    ("HDFC Bank", "hdfc.bank.in", "https://hdfc.bank.in/*", "Banking - India"),
    ("SBI", "onlinesbi.sbi", "https://www.onlinesbi.sbi/*", "Banking - India"),
    ("ICICI Bank", "icicibank.com", "https://www.icicibank.com/*", "Banking - India"),
]

def seed():
    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        # Seed Apps
        for name, process_name, category in APPLICATIONS:
            existing = db.query(ScreenshotApplication).filter_by(name=name).first()
            if not existing:
                app = ScreenshotApplication(name=name, process_name=process_name, category=category)
                db.add(app)
        
        # Seed URLs
        for name, domain, url_pattern, category in URLS:
            existing = db.query(ScreenshotUrl).filter_by(name=name).first()
            if not existing:
                url = ScreenshotUrl(name=name, domain=domain, url_pattern=url_pattern, category=category)
                db.add(url)
        
        db.commit()
        print("Successfully seeded screenshot applications and URLs.")
    finally:
        db.close()

if __name__ == "__main__":
    seed()
